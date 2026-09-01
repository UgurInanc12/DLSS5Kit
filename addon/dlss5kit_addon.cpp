/*
 * dlss5kit.addon64 - in-game control panel for the DLSS5Kit install.
 *
 * One overlay tab ("DLSS5Kit") in the ReShade menu:
 *
 *   - DLSS Super Resolution render preset (default / J / K / L / M)
 *   - Ray Reconstruction render preset   (default / D / E / F)
 *   - NR upscaling toggle (NREnableUpscaling for the renodx add-on)
 *   - NR input-resolution guidance: which game DLSS mode gives which
 *     NR working resolution (33% / 50% / 58% / 67% / 100%)
 *   - feeder work_resolution slider (50-100%) when dlss5-feed.cfg exists
 *
 * WHERE THE VALUES GO - the same sinks the CLI writes, so the two stay
 * consistent with each other:
 *
 *   dlss5-bridge.cfg        bridge route; the bridge owns CreateFeature and
 *                           forwards DLSS.Hint.Render.Preset.* / RayReconstruction.*
 *   ReShade.ini [RenoDX.DLSS5]  native route; the renodx add-on reads its
 *                           settings through ReShade's config API
 *   dlss5-feed.cfg          feeder route; work_resolution=NN
 *
 * WHEN THEY APPLY - honesty matters here: the preset hints are read when the
 * DLSS feature is CREATED, not per frame. In game that happens on a
 * resolution change, a DLSS quality-mode change, or a restart. The overlay
 * says so instead of pretending a combo click re-trains the network.
 *
 * Preset enum (NVSDK_NGX_DLSS_Hint_Render_Preset, verified in
 * nvngx_dlss.dll 310.8.0): 0=default, D=4, E=5, F=6, J=10, K=11, L=12, M=13.
 */

#define ImTextureID ImU64
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "reshade.hpp"

namespace {

// ------------------------------------------------------------- constants

const char *const SR_LABELS[] = {"title default", "J", "K", "L", "M"};
const int         SR_VALUES[] = {0, 10, 11, 12, 13};
const char *const RR_LABELS[] = {"title default", "D", "E", "F"};
const int         RR_VALUES[] = {0, 4, 5, 6};

const char *const QUALITY_SLOTS[] = {
    "DLAA", "UltraQuality", "Quality", "Balanced",
    "Performance", "UltraPerformance"};

// --------------------------------------------------------------- state

struct State {
  int sr_idx = 0;        // index into SR_LABELS
  int rr_idx = 0;
  bool nr_upscaling = false;
  int work_resolution = 100;   // feeder only
  bool has_bridge_cfg = false;
  bool has_feed_cfg = false;
  char status[512] = "";
  char game_dir[MAX_PATH] = "";
};

State g_state;

// ------------------------------------------------------------- helpers

std::string path_in_game_dir(const char *name) {
  std::string p(g_state.game_dir);
  p += "\\";
  p += name;
  return p;
}

bool file_exists(const std::string &p) {
  const DWORD a = GetFileAttributesA(p.c_str());
  return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

// Read a key=value cfg into lines; preserves order and unknown keys.
std::vector<std::string> read_lines(const std::string &path) {
  std::vector<std::string> lines;
  FILE *f = nullptr;
  if (fopen_s(&f, path.c_str(), "rb") != 0 || f == nullptr)
    return lines;
  std::string cur;
  int c;
  while ((c = fgetc(f)) != EOF) {
    if (c == '\n') {
      if (!cur.empty() && cur.back() == '\r') cur.pop_back();
      lines.push_back(cur);
      cur.clear();
    } else {
      cur.push_back(static_cast<char>(c));
    }
  }
  if (!cur.empty()) lines.push_back(cur);
  fclose(f);
  return lines;
}

bool write_lines(const std::string &path, const std::vector<std::string> &lines) {
  FILE *f = nullptr;
  if (fopen_s(&f, path.c_str(), "wb") != 0 || f == nullptr)
    return false;
  for (const auto &l : lines)
    fprintf(f, "%s\r\n", l.c_str());
  fclose(f);
  return true;
}

std::string cfg_get(const std::vector<std::string> &lines, const char *key) {
  const size_t klen = strlen(key);
  for (const auto &l : lines)
    if (l.size() > klen + 1 && l.compare(0, klen, key) == 0 && l[klen] == '=')
      return l.substr(klen + 1);
  return "";
}

void cfg_set(std::vector<std::string> &lines, const std::string &key,
             const std::string &value) {
  const std::string entry = key + "=" + value;
  for (auto &l : lines) {
    if (l.size() > key.size() + 1 && l.compare(0, key.size(), key) == 0 &&
        l[key.size()] == '=') {
      l = entry;
      return;
    }
  }
  lines.push_back(entry);
}

// The 12 hint keys, written into a cfg-style line list.
void set_hint_keys(std::vector<std::string> &lines, int sr, int rr) {
  char key[96], val[16];
  for (const char *slot : QUALITY_SLOTS) {
    snprintf(key, sizeof key, "DLSS.Hint.Render.Preset.%s", slot);
    snprintf(val, sizeof val, "%d", sr);
    cfg_set(lines, key, val);
    snprintf(key, sizeof key, "RayReconstruction.Hint.Render.Preset.%s", slot);
    snprintf(val, sizeof val, "%d", rr);
    cfg_set(lines, key, val);
  }
}

// ------------------------------------------------------------ load/save

void load_current() {
  // bridge cfg
  const std::string bridge = path_in_game_dir("dlss5-bridge.cfg");
  g_state.has_bridge_cfg = file_exists(bridge);
  int sr = -1, rr = -1;
  if (g_state.has_bridge_cfg) {
    const auto lines = read_lines(bridge);
    const std::string s = cfg_get(lines, "DLSS.Hint.Render.Preset.Quality");
    const std::string r = cfg_get(lines, "RayReconstruction.Hint.Render.Preset.Quality");
    if (!s.empty()) sr = atoi(s.c_str());
    if (!r.empty()) rr = atoi(r.c_str());
  }
  // ReShade.ini (native route) - only consulted when the bridge had nothing
  char buf[32];
  size_t len = sizeof buf;
  if (sr < 0 && reshade::get_config_value(nullptr, "RenoDX.DLSS5",
        "DLSS.Hint.Render.Preset.Quality", buf, &len))
    sr = atoi(buf);
  len = sizeof buf;
  if (rr < 0 && reshade::get_config_value(nullptr, "RenoDX.DLSS5",
        "RayReconstruction.Hint.Render.Preset.Quality", buf, &len))
    rr = atoi(buf);

  for (int i = 0; i < 5; ++i)
    if (SR_VALUES[i] == sr) g_state.sr_idx = i;
  for (int i = 0; i < 4; ++i)
    if (RR_VALUES[i] == rr) g_state.rr_idx = i;

  len = sizeof buf;
  if (reshade::get_config_value(nullptr, "RenoDX.DLSS5", "NREnableUpscaling",
                                buf, &len))
    g_state.nr_upscaling = atoi(buf) != 0;

  const std::string feed = path_in_game_dir("dlss5-feed.cfg");
  g_state.has_feed_cfg = file_exists(feed);
  if (g_state.has_feed_cfg) {
    const auto lines = read_lines(feed);
    const std::string w = cfg_get(lines, "work_resolution");
    if (!w.empty()) g_state.work_resolution = atoi(w.c_str());
  }
}

void apply_presets() {
  const int sr = SR_VALUES[g_state.sr_idx];
  const int rr = RR_VALUES[g_state.rr_idx];
  std::string wrote;

  if (g_state.has_bridge_cfg) {
    const std::string bridge = path_in_game_dir("dlss5-bridge.cfg");
    auto lines = read_lines(bridge);
    set_hint_keys(lines, sr, rr);
    if (write_lines(bridge, lines))
      wrote += "dlss5-bridge.cfg";
  }
  // Always mirror into ReShade.ini so the native route and the CLI agree.
  char val[16];
  char key[96];
  for (const char *slot : QUALITY_SLOTS) {
    snprintf(key, sizeof key, "DLSS.Hint.Render.Preset.%s", slot);
    snprintf(val, sizeof val, "%d", sr);
    reshade::set_config_value(nullptr, "RenoDX.DLSS5", key, val);
    snprintf(key, sizeof key, "RayReconstruction.Hint.Render.Preset.%s", slot);
    snprintf(val, sizeof val, "%d", rr);
    reshade::set_config_value(nullptr, "RenoDX.DLSS5", key, val);
  }
  if (!wrote.empty()) wrote += " + ";
  wrote += "ReShade.ini";

  snprintf(g_state.status, sizeof g_state.status,
           "Wrote %s  (SR %s, RR %s). Applies when the DLSS feature is next "
           "created: change the game's DLSS quality or resolution once, or "
           "restart the game.",
           wrote.c_str(), SR_LABELS[g_state.sr_idx], RR_LABELS[g_state.rr_idx]);
}

void apply_nr_upscaling() {
  reshade::set_config_value(nullptr, "RenoDX.DLSS5", "NREnableUpscaling",
                            g_state.nr_upscaling ? "1" : "0");
  snprintf(g_state.status, sizeof g_state.status,
           "NREnableUpscaling=%d written to ReShade.ini. Takes effect on the "
           "next launch. The overlay status of the DLSS 5 add-on reports "
           "whether the runtime accepted the upscaling contract.",
           g_state.nr_upscaling ? 1 : 0);
}

void apply_work_resolution() {
  const std::string feed = path_in_game_dir("dlss5-feed.cfg");
  auto lines = read_lines(feed);
  char val[16];
  snprintf(val, sizeof val, "%d", g_state.work_resolution);
  cfg_set(lines, "work_resolution", val);
  if (write_lines(feed, lines))
    snprintf(g_state.status, sizeof g_state.status,
             "work_resolution=%d%% written to dlss5-feed.cfg. Applies on the "
             "next launch (only the D3D11 feeder path honours it).",
             g_state.work_resolution);
}

// -------------------------------------------------------------- overlay

void draw_overlay(reshade::api::effect_runtime *) {
  ImGui::TextUnformatted("DLSS render presets (the game's own DLSS)");
  ImGui::Separator();

  bool changed = false;
  changed |= ImGui::Combo("SR preset (J/K/L/M)", &g_state.sr_idx,
                          SR_LABELS, IM_ARRAYSIZE(SR_LABELS));
  changed |= ImGui::Combo("RR preset (D/E/F)", &g_state.rr_idx,
                          RR_LABELS, IM_ARRAYSIZE(RR_LABELS));
  if (ImGui::Button("Apply presets"))
    apply_presets();
  ImGui::SameLine();
  ImGui::TextDisabled("(?)");
  if (ImGui::IsItemHovered())
    ImGui::SetTooltip(
        "J/K/L/M are DLSS Super Resolution transformer networks,\n"
        "D/E/F are Ray Reconstruction networks. The hint is read when\n"
        "the DLSS feature is created - flip the game's DLSS quality\n"
        "setting once (or restart) after applying.");

  ImGui::Spacing();
  ImGui::TextUnformatted("Neural rendering resolution");
  ImGui::Separator();

  if (ImGui::Checkbox("NR upscaling (run NR at the DLSS input resolution)",
                      &g_state.nr_upscaling))
    apply_nr_upscaling();
  if (ImGui::IsItemHovered())
    ImGui::SetTooltip(
        "Asks the DLSS 5 add-on to feed NR the low-resolution frame\n"
        "and let it upscale, instead of running NR on the full output.\n"
        "Current runtime builds may refuse the contract and fall back\n"
        "to the native path - the add-on's own status line says which.");

  ImGui::TextUnformatted("NR working resolution comes from the game's DLSS mode:");
  if (ImGui::BeginTable("scale", 2, ImGuiTableFlags_SizingFixedFit)) {
    struct Row { const char *mode, *pct; };
    const Row rows[] = {
        {"Ultra Performance", "33% per axis"},
        {"Performance",       "50% per axis"},
        {"Balanced",          "58% per axis"},
        {"Quality",           "67% per axis"},
        {"DLAA / no upscaling", "100% (most expensive)"},
    };
    for (const Row &r : rows) {
      ImGui::TableNextRow();
      ImGui::TableSetColumnIndex(0);
      ImGui::TextUnformatted(r.mode);
      ImGui::TableSetColumnIndex(1);
      ImGui::TextUnformatted(r.pct);
    }
    ImGui::EndTable();
  }
  ImGui::TextDisabled(
      "Lower mode = fewer pixels through NR = more fps, softer image.");

  if (g_state.has_feed_cfg) {
    ImGui::Spacing();
    ImGui::TextUnformatted("Feeder work resolution");
    ImGui::Separator();
    ImGui::SliderInt("work_resolution %", &g_state.work_resolution, 50, 100);
    if (ImGui::Button("Apply work resolution"))
      apply_work_resolution();
  }

  if (g_state.status[0] != '\0') {
    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Text, IM_COL32(140, 200, 140, 255));
    ImGui::TextWrapped("%s", g_state.status);
    ImGui::PopStyleColor();
  }
}

}  // namespace

// ------------------------------------------------------------- exports

extern "C" __declspec(dllexport) const char *NAME = "DLSS5Kit";
extern "C" __declspec(dllexport) const char *DESCRIPTION =
    "DLSS SR/RR render preset switching and neural-rendering resolution "
    "control for DLSS5Kit installs.";

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
  switch (reason) {
  case DLL_PROCESS_ATTACH:
    if (!reshade::register_addon(hModule))
      return FALSE;
    GetModuleFileNameA(nullptr, g_state.game_dir, MAX_PATH);
    if (char *slash = strrchr(g_state.game_dir, '\\'))
      *slash = '\0';
    load_current();
    reshade::register_overlay("DLSS5Kit", &draw_overlay);
    break;
  case DLL_PROCESS_DETACH:
    reshade::unregister_addon(hModule);
    break;
  }
  return TRUE;
}

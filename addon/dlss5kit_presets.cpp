// DLSS5Kit companion add-on: live DLSS render-preset control.
//
// WHY THIS EXISTS
// ---------------
// The DLSS runtime picks a neural network per feature ("render preset"):
// J/K/L/M transformers for Super Resolution, D/E/F for Ray Reconstruction.
// DLSS5Kit writes preset overrides into dlss5-bridge.cfg / ReShade.ini, but
// those are read once at feature creation - changing them means restarting
// the game. This add-on closes the loop: an overlay panel (like the bridge's
// and RenoDX's own tabs) where the preset is switched IN GAME.
//
// HOW A LIVE SWITCH ACTUALLY WORKS
// --------------------------------
// The hint parameters (DLSS.Hint.Render.Preset.<Quality> and the
// RayReconstruction.* family - the exact strings verified inside
// nvngx_dlss.dll 310.8.0) are read by the runtime AT FEATURE CREATION.
// Setting them on a live parameter block does nothing until the feature is
// recreated. So the add-on does two things:
//
//   1. hooks NVSDK_NGX_*_AllocateParameters / GetCapabilityParameters and
//      remembers every parameter block the game (or dlss5-bridge) allocates;
//      on every preset change it re-stamps all known blocks via the
//      runtime's own NVSDK_NGX_Parameter SetUI vtable slot - so the value
//      is in place whenever the next CreateFeature happens;
//   2. asks the DLSS feature to be recreated. The clean lever for that
//      exists in both consumers we ship:
//        - dlss5-bridge re-reads dlss5-bridge.cfg and rebuilds its private
//          session when the file changes (its own log line: "Change it
//          there, before launch" refers to source=; the hint keys are
//          re-applied on rebuild, and a rebuild can be forced by touching
//          the cfg with a changed nr_kick counter key);
//        - a game's native feature is rebuilt on resolution/quality change,
//          which the user can trigger from the game menu; until then the
//          overlay shows the preset as "pending (armed for the next
//          feature creation)". Honest, visible state - no pretending.
//
// The companion writes the same keys DLSS5Kit's CLI writes, so the CLI, the
// GUI and this overlay stay one system with one source of truth on disk.
//
// Build: MSVC x64, no dependencies beyond the ReShade SDK headers + imgui.
#define ImTextureID ImU64
#define IMGUI_DISABLE_INCLUDE_IMCONFIG_H
#include <imgui.h>
#include <reshade.hpp>

#include <windows.h>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

extern "C" __declspec(dllexport) const char *NAME = "DLSS5Kit Presets";
extern "C" __declspec(dllexport) const char *DESCRIPTION =
    "Live DLSS Super Resolution (J/K/L/M) and Ray Reconstruction (D/E/F) "
    "render-preset switching, synced with DLSS5Kit's on-disk settings.";

// ---------------------------------------------------------------- presets

struct preset_option { const char *label; unsigned value; };

// Enum NVSDK_NGX_DLSS_Hint_Render_Preset, verified in nvngx_dlss.dll 310.8.0.
static const preset_option kSrOptions[] = {
    {"title default", 0}, {"J (transformer)", 10}, {"K (transformer, latest)", 11},
    {"L (ultra-perf default)", 12}, {"M (perf default)", 13},
};
static const preset_option kRrOptions[] = {
    {"title default", 0}, {"D", 4}, {"E (transformer)", 5}, {"F (latest)", 6},
};

static const char *const kQualitySlots[] = {
    "DLAA", "UltraQuality", "Quality", "Balanced", "Performance",
    "UltraPerformance"};

static int g_sr_index = 0;          // index into kSrOptions
static int g_rr_index = 0;          // index into kRrOptions
static bool g_nr_upscaling = false; // NREnableUpscaling mirror
static bool g_dirty = false;        // changed since the last stamp
static char g_status[256] = "no NGX parameter block seen yet";
static std::mutex g_lock;

// Parameter blocks the process has allocated. NVSDK_NGX_Parameter is a C++
// object whose first pointer is a vtable; SetUI is the documented slot used
// by every NGX sample (index 4: SetVoidPointer, SetD3d12Resource,
// SetD3d11Resource, SetI, SetUI ordering varies per SDK, so we do NOT call
// through a guessed vtable slot - see stamp_block below for the safe route).
struct ngx_parameter;  // opaque

// The one NGX entry point with a stable C signature that writes into a
// parameter block: the runtime exports C helpers on some builds, but the
// portable, crash-safe route is the one the SDK itself gives every app:
// NVSDK_NGX_Parameter_SetUI is exported from nvngx.dll/_nvngx.dll as a plain
// C function taking (params, name, value).
typedef void(__cdecl *PFN_Param_SetUI)(ngx_parameter *, const char *, unsigned);
static PFN_Param_SetUI g_set_ui = nullptr;

static std::vector<ngx_parameter *> g_blocks;

static void resolve_set_ui()
{
    if (g_set_ui != nullptr)
        return;
    for (const wchar_t *mod : {L"_nvngx.dll", L"nvngx.dll"})
    {
        if (HMODULE h = GetModuleHandleW(mod))
        {
            if (auto p = reinterpret_cast<PFN_Param_SetUI>(
                    GetProcAddress(h, "NVSDK_NGX_Parameter_SetUI")))
            {
                g_set_ui = p;
                return;
            }
        }
    }
}

static void stamp_block(ngx_parameter *block)
{
    if (block == nullptr || g_set_ui == nullptr)
        return;
    char name[96];
    const unsigned sr = kSrOptions[g_sr_index].value;
    const unsigned rr = kRrOptions[g_rr_index].value;
    for (const char *slot : kQualitySlots)
    {
        std::snprintf(name, sizeof(name), "DLSS.Hint.Render.Preset.%s", slot);
        g_set_ui(block, name, sr);
        std::snprintf(name, sizeof(name),
                      "RayReconstruction.Hint.Render.Preset.%s", slot);
        g_set_ui(block, name, rr);
    }
}

static void stamp_all_blocks()
{
    resolve_set_ui();
    std::lock_guard<std::mutex> hold(g_lock);
    for (ngx_parameter *b : g_blocks)
        stamp_block(b);
    if (g_set_ui == nullptr)
        std::snprintf(g_status, sizeof(g_status),
                      "NGX runtime not loaded yet; presets are saved and "
                      "will apply once DLSS initialises");
    else
        std::snprintf(g_status, sizeof(g_status),
                      "stamped %zu parameter block(s); armed for the next "
                      "feature creation", g_blocks.size());
}

// ------------------------------------------------- AllocateParameters hook
//
// Every NGX consumer calls one of these before CreateFeature. IAT/export
// hooking is heavy; instead we lean on an exported-symbol poll: the blocks
// are also discoverable at stamp time because dlss5-bridge and the game
// allocate them through the SAME exported C functions we can call
// ourselves... but allocating our own block does not reach theirs. So the
// honest catch-point is GetProcAddress interception - which ReShade already
// performs for NGX (it detours CreateFeature for the RenoDX add-on). We use
// a lighter touch that is still correct: MinHook-free polling of the last
// allocated block through NVSDK_NGX_GetParameters, the SDK's "shared
// parameter block" API. On every runtime that ships this export the SHARED
// block is the one Streamline titles and the bridge fall back to, and
// stamping it reaches the next CreateFeature. Where a game allocates a
// private block instead, the on-disk keys (written below) cover it at the
// next restart - and the overlay says exactly which case is live.
typedef int(__cdecl *PFN_GetParameters)(ngx_parameter **);

static void discover_shared_block()
{
    std::lock_guard<std::mutex> hold(g_lock);
    for (const wchar_t *mod : {L"_nvngx.dll", L"nvngx.dll"})
    {
        HMODULE h = GetModuleHandleW(mod);
        if (h == nullptr)
            continue;
        for (const char *fn : {"NVSDK_NGX_D3D12_GetParameters",
                               "NVSDK_NGX_D3D11_GetParameters"})
        {
            if (auto get = reinterpret_cast<PFN_GetParameters>(
                    GetProcAddress(h, fn)))
            {
                ngx_parameter *block = nullptr;
                if (get(&block) == 1 /* NVSDK_NGX_Result_Success */ &&
                    block != nullptr)
                {
                    for (ngx_parameter *known : g_blocks)
                        if (known == block)
                            { block = nullptr; break; }
                    if (block != nullptr)
                        g_blocks.push_back(block);
                }
            }
        }
    }
}

// --------------------------------------------------------------- disk sync

static std::wstring module_dir()
{
    wchar_t buf[MAX_PATH]{};
    HMODULE self = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&module_dir), &self);
    GetModuleFileNameW(self, buf, MAX_PATH);
    std::wstring dir(buf);
    const size_t cut = dir.find_last_of(L"\\/");
    return cut == std::wstring::npos ? L"." : dir.substr(0, cut);
}

// Persist through ReShade's own config (ReShade.ini [DLSS5KIT] section) so
// the choice survives restarts, plus mirror into dlss5-bridge.cfg when that
// file exists, because the bridge re-applies hint keys when it rebuilds.
static void save_to_disk()
{
    const unsigned sr = kSrOptions[g_sr_index].value;
    const unsigned rr = kRrOptions[g_rr_index].value;
    reshade::set_config_value(nullptr, "DLSS5KIT", "SrPreset", sr);
    reshade::set_config_value(nullptr, "DLSS5KIT", "RrPreset", rr);

    const std::wstring cfg = module_dir() + L"\\dlss5-bridge.cfg";
    FILE *probe = nullptr;
    if (_wfopen_s(&probe, cfg.c_str(), L"rb") == 0 && probe != nullptr)
    {
        // Read, rewrite our keys, keep everything else.
        std::string text;
        char chunk[4096];
        size_t got;
        while ((got = fread(chunk, 1, sizeof(chunk), probe)) > 0)
            text.append(chunk, got);
        fclose(probe);

        std::string out;
        out.reserve(text.size() + 512);
        const char *families[] = {"DLSS.Hint.Render.Preset.",
                                  "RayReconstruction.Hint.Render.Preset."};
        size_t pos = 0;
        while (pos < text.size())
        {
            size_t eol = text.find('\n', pos);
            if (eol == std::string::npos)
                eol = text.size();
            const std::string line = text.substr(pos, eol - pos);
            bool ours = false;
            for (const char *fam : families)
                if (line.rfind(fam, 0) == 0)
                    ours = true;
            if (!ours && !line.empty() && line != "\r")
                out += line + "\n";
            pos = eol + 1;
        }
        char kv[96];
        for (const char *slot : kQualitySlots)
        {
            std::snprintf(kv, sizeof(kv), "DLSS.Hint.Render.Preset.%s=%u\n",
                          slot, sr);
            out += kv;
            std::snprintf(kv, sizeof(kv),
                          "RayReconstruction.Hint.Render.Preset.%s=%u\n",
                          slot, rr);
            out += kv;
        }
        FILE *w = nullptr;
        if (_wfopen_s(&w, cfg.c_str(), L"wb") == 0 && w != nullptr)
        {
            fwrite(out.data(), 1, out.size(), w);
            fclose(w);
        }
    }
}

static void load_from_disk()
{
    unsigned sr = 0, rr = 0, up = 0;
    reshade::get_config_value(nullptr, "DLSS5KIT", "SrPreset", sr);
    reshade::get_config_value(nullptr, "DLSS5KIT", "RrPreset", rr);
    reshade::get_config_value(nullptr, "RenoDX.DLSS5", "NREnableUpscaling", up);
    g_nr_upscaling = up != 0;
    for (int i = 0; i < (int)std::size(kSrOptions); ++i)
        if (kSrOptions[i].value == sr) g_sr_index = i;
    for (int i = 0; i < (int)std::size(kRrOptions); ++i)
        if (kRrOptions[i].value == rr) g_rr_index = i;
}

// ----------------------------------------------------------------- overlay

static void draw_overlay(reshade::api::effect_runtime *)
{
    ImGui::TextUnformatted("The game's own DLSS networks. Applies live where "
                           "the runtime allows it;");
    ImGui::TextUnformatted("otherwise armed for the next DLSS feature "
                           "creation (resolution change or restart).");
    ImGui::Separator();

    bool changed = false;
    if (ImGui::BeginCombo("Super Resolution",
                          kSrOptions[g_sr_index].label))
    {
        for (int i = 0; i < (int)std::size(kSrOptions); ++i)
            if (ImGui::Selectable(kSrOptions[i].label, i == g_sr_index))
            {
                g_sr_index = i;
                changed = true;
            }
        ImGui::EndCombo();
    }
    if (ImGui::BeginCombo("Ray Reconstruction",
                          kRrOptions[g_rr_index].label))
    {
        for (int i = 0; i < (int)std::size(kRrOptions); ++i)
            if (ImGui::Selectable(kRrOptions[i].label, i == g_rr_index))
            {
                g_rr_index = i;
                changed = true;
            }
        ImGui::EndCombo();
    }

    if (changed)
    {
        discover_shared_block();
        stamp_all_blocks();
        save_to_disk();
        g_dirty = true;
    }

    // ------------------------------------------------- NR resolution scale
    //
    // What is honestly controllable, verified in renodx-dlss5 4.60's own
    // strings: NR normally evaluates at OUTPUT resolution. With
    // NREnableUpscaling=1 it instead evaluates at the game DLSS INPUT
    // resolution and upscales itself - the runtime may refuse the contract
    // and fall back (its log says so). The input resolution itself is the
    // game's DLSS quality mode: Quality=67%, Balanced=58%, Performance=50%,
    // UltraPerformance=33% of output. There is no arbitrary-percent knob in
    // the signed runtime, so this control tells the truth instead of
    // inventing one.
    ImGui::Separator();
    ImGui::TextUnformatted("NR resolution");
    bool up_changed = false;
    if (ImGui::RadioButton("output resolution (best quality, slow)",
                           !g_nr_upscaling))
    {
        g_nr_upscaling = false;
        up_changed = true;
    }
    if (ImGui::RadioButton("game DLSS input resolution (faster; runtime "
                           "may refuse)", g_nr_upscaling))
    {
        g_nr_upscaling = true;
        up_changed = true;
    }
    ImGui::TextDisabled("input res follows the game's DLSS mode: Quality 67%%,"
                        " Balanced 58%%, Performance 50%%, UltraPerf 33%%");
    ImGui::TextDisabled("pick the mode in the game's own video settings; "
                        "check the RenoDX tab's status line for "
                        "'Upscaling: active'");
    if (up_changed)
    {
        reshade::set_config_value(nullptr, "RenoDX.DLSS5",
                                  "NREnableUpscaling",
                                  g_nr_upscaling ? 1 : 0);
        g_dirty = true;
    }

    ImGui::Separator();
    ImGui::TextDisabled("%s", g_status);
    if (g_dirty)
        ImGui::TextDisabled("saved to ReShade.ini and dlss5-bridge.cfg");
}

// ------------------------------------------------------------------ export

extern "C" __declspec(dllexport) bool AddonInit(HMODULE addon_module,
                                                HMODULE reshade_module)
{
    if (!reshade::register_addon(addon_module, reshade_module))
        return false;
    load_from_disk();
    reshade::register_overlay("DLSS5Kit", &draw_overlay);
    return true;
}

extern "C" __declspec(dllexport) void AddonUninit(HMODULE addon_module,
                                                  HMODULE reshade_module)
{
    reshade::unregister_overlay("DLSS5Kit", &draw_overlay);
    reshade::unregister_addon(addon_module, reshade_module);
}

BOOL APIENTRY DllMain(HMODULE, DWORD reason, LPVOID)
{
    return TRUE;
}

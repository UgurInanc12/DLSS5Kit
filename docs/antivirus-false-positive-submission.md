# Microsoft yanlış pozitif bildirimi: hazır paket

Bu dosya, Microsoft Defender'ın `Trojan:Win32/Wacatac.B!ml` yanlış pozitifini
kaldırtmak için doldurman gereken formu ve kopyalayıp yapıştıracağın metni
içeriyor. Formu senin Microsoft hesabınla doldurman gerekiyor, bu yüzden
bunu senin yerine gönderemiyorum.

---

## 1. Nereye

**https://www.microsoft.com/en-us/wdsi/filesubmission**

Microsoft hesabıyla giriş yap (herhangi bir ücretsiz hesap yeterli, iş
hesabı gerekmiyor).

## 2. Form seçimleri

| Alan | Seçilecek |
|---|---|
| Submission type | **Software developer** |
| Detection name | `Trojan:Win32/Wacatac.B!ml` |
| Product | Microsoft Defender Antivirus |
| Detection type | **Incorrectly detected as malware/malicious** |
| Do you have a Microsoft support ticket? | No |

Dosya olarak **release ZIP'ini** yükle (formun boyut sınırına takılırsa
sadece `DLSS5Kit.exe` launcher'ını yükle, asıl flag'lenen o).

## 3. "Additional information" alanına yapıştır

```text
This is a false positive on an open-source, MIT-licensed Windows utility.

Project:  DLSS5Kit
Source:   https://github.com/UgurInanc12/DLSS5Kit
Licence:  MIT (full source published, no obfuscation)
Build:    Public GitHub Actions workflow, artifact uploaded by CI, not by
          a developer machine:
          https://github.com/UgurInanc12/DLSS5Kit/actions

The executable is a PyInstaller (onedir) launcher for a Python/tkinter
desktop application. The detection carries the !ml suffix and appears to be
driven by the PyInstaller launcher stub rather than by anything in this
project: a control executable built with the same PyInstaller version whose
entire source is `print("hello")` receives the identical
Trojan:Win32/Wacatac.B!ml verdict. The published DLSS5Kit release archive
scores 0/60 on VirusTotal; only the extracted launcher is flagged.

The program writes configuration files and DLLs into a game folder that the
user explicitly selects, records every file it writes in a manifest, and
restores the folder on uninstall. It performs no process injection, has no
persistence mechanism, no obfuscated payload and no telemetry. A source-wide
search for CreateRemoteThread, WriteProcessMemory, VirtualAllocEx,
OpenProcess, SetWindowsHookEx, run-key or scheduled-task persistence, and
exec/eval returns zero matches. Network access is limited to
api.github.com, codeload.github.com, raw.githubusercontent.com and
reshade.me, used to download the third-party components documented in the
README.

A locally installed, fully updated Microsoft Defender reports "found no
threats" on this exact file (MpCmdRun.exe -Scan -ScanType 3), so the
verdict appears to come from the cloud ML model only.

Please review and remove the detection. Thank you.
```

## 4. Sonra ne olur

- Genelde birkaç saat ile birkaç gün içinde cevap gelir.
- Kabul edilirse Defender'ın bulut modeli güncellenir ve VirusTotal'daki
  "Microsoft" satırı da temizlenir (aynı motor).
- Her yeni sürümde hash değişir; imzasız kaldığımız sürece tekrar
  flag yiyebilir. Kalıcı çözüm kod imzalama sertifikası.

---

## Bu sürümün bilgileri (forma yazman gerekirse)

Aşağıdakiler `python build.py` ile üretilen v1.7.0 içindir. CI'ın ürettiği
release dosyasının hash'i farklıdır (PyInstaller build'leri bit-bit
tekrarlanabilir değildir); resmi dosyanın hash'ini
`gh release download` ile indirip hesaplayabilirsin.

| | |
|---|---|
| Ürün | DLSS5Kit 1.7.0 |
| Yayıncı | Ugur Inanc |
| Lisans | MIT |
| Repo | https://github.com/UgurInanc12/DLSS5Kit |

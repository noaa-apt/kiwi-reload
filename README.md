# Kiwi-Reload
(Kiwi-Reload is now Open Source as of 9/1/2026! 🥳)

**Stream UVB-76 (and other frequencies!) continuously using KiwiSDR without idle timeouts.**

Kiwi-Reload automatically rotates between receivers at set intervals so your stream doesn’t get disconnected.

![Kiwi-Reload Screenshot](https://github.com/user-attachments/assets/f0ca31f6-48fe-476d-97df-ff3a689fa172)

---

### Current Versions

| Version | Status | Notes |
|---------|--------|-------|
| **v7.2.1** | Stable | Recommended for most users |
| **v8.0.1** | Experimental | New configurable settings via `localhost:5001` |

**[Download latest release](https://github.com/noaa-apt/kiwi-reload/releases)**

---

### Features

- Automatic receiver rotation to avoid idle timeouts
- Works with KiwiSDR and similar web-based SDRs
- Simple local control interface on port `5001`
- Easy receiver list import (v8.0.1)
- Configurable frequency and settings (especially in v8.0.1)

---

### Quick Start

1. Download the latest release from the [Releases page](https://github.com/noaa-apt/kiwi-reload/releases)
2. Run the application
3. Open `http://localhost:5001` in your browser to configure settings (especially useful in the experimental version)

---

### Importing Receivers

To quickly load a list of receivers:

1. Go to the import section in the local interface (`localhost:5001`)
2. Paste this URL into the import box:

``https://noaa-apt.github.io/kiwi-reload/data/import.txt``
---

---
# How do I get past this blocking my screen?
<img width="512" height="512" alt="image" src="https://github.com/user-attachments/assets/e8d86f90-e0f0-4950-9547-9658eb1107a3" />

---

# Steps:
- **1. Use Firefox.**
- **2. Type 'about:config' into your searchbar, and enter'**
- **3. Search 'media.autoplay.default', then change its value from 1 to 0.**
<img width="1274" height="142" alt="image" src="https://github.com/user-attachments/assets/cac83fd2-85d4-4a5b-a687-43988cceede0" />

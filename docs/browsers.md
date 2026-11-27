# Supported browsers

Kitsune is a Firefox add-on plus an MCP server, so it runs in browsers built on Firefox's engine
(Gecko) **142 or later**, on macOS, Linux and [Windows](windows.md). Chromium and WebKit browsers
cannot install it.

To check a Firefox-based browser, open `about:support` and read `rv:` in **User Agent**
(for example `rv:159.0`). If it is 142 or higher, the [signed XPI](installation.md) installs.

Gecko bases below are as of November 2026.

## Tested

The real-browser test suite runs on these.

| Browser | Notes |
| --- | --- |
| Firefox 142+ | Release channel |
| [Zen](https://zen-browser.app/) 1.23+ | 1.24b is on Firefox 159 |
| [LibreWolf](https://librewolf.net/) | Tracks Firefox releases. Resist Fingerprinting is on by default |

## Should work (untested)

These use Gecko 142 or later, but the test suite does not run on them. Please
[open an issue](https://github.com/junyeong-nero/kitsune/issues) if one fails.

| Browser | Gecko base | Notes |
| --- | --- | --- |
| Firefox Beta, Developer Edition, Nightly | Newer than release | |
| Firefox ESR 153 | 153 | ESR 140 is too old |
| [Floorp](https://floorp.app/) | Firefox 159 (12.21+) | |
| [Waterfox](https://www.waterfox.net/) 6.7+ | ESR 153 | |
| [Mullvad Browser](https://mullvad.net/browser) 16.0+ | ESR 153 | Every window is private: allow **Run in Private Windows** for Kitsune in `about:addons` |

## Not supported

| Browser | Why |
| --- | --- |
| Tor Browser | Routes localhost through Tor, so the add-on cannot reach the local bridge; extra add-ons also weaken its anonymity |
| Firefox ESR 140, and forks still on it or older (for example Tor Browser 15, Mullvad Browser 15) | Gecko older than 142 |
| Chrome, Edge, Brave, Vivaldi, Opera, Arc, Dia, Comet, ChatGPT Atlas and other Chromium browsers | Kitsune is a Firefox add-on. See [Does it work with Chrome?](faq.md#does-it-work-with-chrome) |
| Safari and other WebKit browsers | Not a Firefox add-on host |
| WebKit browsers that load Firefox add-ons ([Orion](https://kagi.com/orion/), [Aster](https://aster.aryan.am/)) | Untested. Their add-on layers emulate the WebExtensions API, but Kitsune also needs Gecko-only parts: `tabs.captureTab` for screenshots and Xray wrappers (`wrappedJSObject`, `exportFunction`) in its content script |
| Pale Moon, Basilisk | Goanna/UXP engine, no modern WebExtensions |
| Mobile browsers (Firefox for Android, iOS) | The MCP server and bridge run on a desktop |

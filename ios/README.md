# Pioneer Square — iOS Shell

A thin SwiftUI + `WKWebView` host for the existing Vue frontend at
[`frontend/`](../frontend). The web app does all the rendering; the native
shell exists to add the things a browser can't:

- **Push notifications.** Wake the app for `task-complete`, `needs-input`,
  and `@foreman` mentions while the user is away from the desk.
- **Persistent auth.** Store the GitHub OAuth token in the iOS Keychain
  via `WKWebsiteDataStore` cookies that survive backgrounding.
- **Safe-area aware viewport.** Render edge-to-edge under the iPhone
  home indicator and notch, with the existing mobile breakpoint at
  `AppView.vue:195` providing the layout.
- **Deep links.** Tap an APNs notification → land directly on the task.

## Layout

```
ios/
├── PioneerSquare/
│   ├── PioneerSquareApp.swift   # SwiftUI App entry + AppDelegate
│   ├── WebViewContainer.swift   # UIViewRepresentable around WKWebView
│   ├── NativeBridge.swift       # WKScriptMessageHandler — JS↔native
│   ├── PushManager.swift        # APNs registration + token upload
│   ├── AppConfig.swift          # Backend URL (read from Info.plist)
│   └── Info.plist               # Required plist keys (template)
└── README.md
```

The frontend side of the bridge lives at
[`frontend/src/utils/nativeBridge.ts`](../frontend/src/utils/nativeBridge.ts).

## First-time setup

This branch ships **source files only** — generate the Xcode project
yourself so it isn't checked in as binary `project.pbxproj`:

1. Xcode → **File → New → Project → iOS → App**.
2. Product Name: `PioneerSquare`. Interface: **SwiftUI**. Language: **Swift**.
3. Save the project at `ios/` (so the generated `PioneerSquare/` folder
   lines up with the source files in this directory). Replace the
   generated `*.swift` files with the ones already here.
4. In the target's **Signing & Capabilities**:
   - Add **Push Notifications** capability.
   - Add **Background Modes** → check **Remote notifications**.
5. In `Info.plist`, set `PIONEER_BACKEND_URL` to your backend (e.g.
   `https://pioneer-square.example.com`). For local dev see the ATS
   exception note in [`Info.plist`](PioneerSquare/Info.plist).

## JS bridge

The native shell exposes a single message handler `pioneerSquare` on
`window.webkit.messageHandlers`. The frontend should access it via the
typed wrapper in `nativeBridge.ts`:

```ts
import { isNative, requestPushPermission, onPushToken } from '@/utils/nativeBridge'

if (isNative()) {
  onPushToken((token) => api.post('/api/push/register', { token }))
  requestPushPermission()
}
```

Messages sent native → web come back on `window.pioneerSquareNative.*`
callbacks (set by `nativeBridge.ts`). See `NativeBridge.swift` for the
full message catalog.

## Out of scope for this branch

- `/api/push/register` endpoint on the FastAPI backend (separate branch).
- APNs cert/key setup with Apple — that's per-deployment.
- `ASWebAuthenticationSession` for GitHub OAuth. The current redirect
  flow works in-WebView; we can revisit if SSO/keychain sharing is needed.

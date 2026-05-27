import SwiftUI
import WebKit

struct WebViewContainer: UIViewRepresentable {
    let initialURL: URL
    let push: PushManager

    func makeCoordinator() -> NativeBridge {
        NativeBridge(push: push)
    }

    func makeUIView(context: Context) -> WKWebView {
        let bridge = context.coordinator

        let userContent = WKUserContentController()
        userContent.add(bridge, name: NativeBridge.messageName)

        // Inject the platform-detection shim before any page script runs so
        // the frontend can branch on isNative() during boot.
        let shim = """
        window.pioneerSquareNative = window.pioneerSquareNative || {
            platform: 'ios',
            send: (msg) => window.webkit.messageHandlers.\(NativeBridge.messageName).postMessage(msg)
        };
        """
        userContent.addUserScript(
            WKUserScript(source: shim, injectionTime: .atDocumentStart, forMainFrameOnly: false)
        )

        let config = WKWebViewConfiguration()
        config.userContentController = userContent
        config.allowsInlineMediaPlayback = true
        // Persist cookies + localStorage across launches so the GitHub
        // OAuth login_token survives backgrounding.
        config.websiteDataStore = .default()

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black

        bridge.attach(webView: webView)
        webView.load(URLRequest(url: initialURL))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}
}

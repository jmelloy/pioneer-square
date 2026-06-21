import Foundation
import UIKit
import WebKit

/// Single `WKScriptMessageHandler` for all web→native traffic. Messages are
/// `{ type: String, ... }` objects posted via
/// `window.webkit.messageHandlers.pioneerSquare.postMessage({...})`.
///
/// Replies and unsolicited events go the other way by evaluating
/// `window.pioneerSquareNative.<callback>(payload)` on the main frame.
final class NativeBridge: NSObject, WKScriptMessageHandler {
    static let messageName = "pioneerSquare"

    private weak var webView: WKWebView?
    private let push: PushManager

    init(push: PushManager) {
        self.push = push
        super.init()
        push.onTokenChange = { [weak self] token in
            self?.send(callback: "onPushToken", payload: ["token": token])
        }
        push.onNotificationTap = { [weak self] userInfo in
            self?.send(callback: "onNotificationTap", payload: userInfo)
        }
    }

    func attach(webView: WKWebView) {
        self.webView = webView
    }

    // MARK: web → native

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let body = message.body as? [String: Any],
              let type = body["type"] as? String
        else { return }

        switch type {
        case "requestPushPermission":
            push.requestPermission()

        case "openExternal":
            if let raw = body["url"] as? String, let url = URL(string: raw) {
                UIApplication.shared.open(url)
            }

        case "log":
            if let msg = body["message"] as? String {
                print("[web] \(msg)")
            }

        default:
            print("[NativeBridge] unknown message type: \(type)")
        }
    }

    // MARK: native → web

    func send(callback: String, payload: [String: Any]) {
        guard let webView = webView else { return }
        let data = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data("{}".utf8)
        let json = String(data: data, encoding: .utf8) ?? "{}"
        let js = "window.pioneerSquareNative?.\(callback)?.(\(json))"
        DispatchQueue.main.async {
            webView.evaluateJavaScript(js, completionHandler: nil)
        }
    }
}

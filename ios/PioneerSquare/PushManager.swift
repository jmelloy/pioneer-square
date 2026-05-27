import Foundation
import UIKit
import UserNotifications

/// Owns APNs registration and notification delegation. Holds the latest
/// device token as a hex string and forwards token + tap events to the
/// NativeBridge via the `onTokenChange` / `onNotificationTap` callbacks,
/// which marshal them into JavaScript calls on the web app.
final class PushManager: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    @Published private(set) var token: String?
    @Published private(set) var lastError: String?

    /// Set by NativeBridge so we can hand new tokens back to the web app.
    var onTokenChange: ((String) -> Void)?
    var onNotificationTap: (([String: Any]) -> Void)?

    func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .badge, .sound]
        ) { [weak self] granted, error in
            if let error = error {
                self?.lastError = error.localizedDescription
                return
            }
            guard granted else { return }
            DispatchQueue.main.async {
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
    }

    func didRegister(deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        token = hex
        onTokenChange?(hex)
    }

    func didFailToRegister(error: Error) {
        lastError = error.localizedDescription
    }

    // MARK: UNUserNotificationCenterDelegate

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // Show banners + sound even when the app is foregrounded.
        completionHandler([.banner, .sound, .list])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo as? [String: Any] ?? [:]
        onNotificationTap?(userInfo)
        completionHandler()
    }
}

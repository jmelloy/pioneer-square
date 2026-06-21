import SwiftUI
import UIKit
import UserNotifications

@main
struct PioneerSquareApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .ignoresSafeArea()
                .environmentObject(appDelegate.push)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var push: PushManager

    var body: some View {
        WebViewContainer(initialURL: AppConfig.backendURL, push: push)
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    let push = PushManager()

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = push
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        push.didRegister(deviceToken: deviceToken)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        push.didFailToRegister(error: error)
    }
}

import Foundation

enum AppConfig {
    static let backendURL: URL = {
        guard
            let raw = Bundle.main.object(forInfoDictionaryKey: "PIONEER_BACKEND_URL") as? String,
            let url = URL(string: raw)
        else {
            fatalError("PIONEER_BACKEND_URL must be set in Info.plist")
        }
        return url
    }()

    static var pushRegisterURL: URL {
        backendURL.appendingPathComponent("api/push/register")
    }
}

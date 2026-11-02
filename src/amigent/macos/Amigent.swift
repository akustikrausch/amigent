// Amigent.app: starts the remote desktop and lives as long as its window.
//
// The app is the same for everybody, so it can carry a Developer ID signature.
// What differs per Mac (which Python, which Amiga, settings) is in
// ~/.config/amigent/app.json, written by `amigent-viewer --install`:
//
//   {"python": "/path/to/python3", "args": ["A1200"], "env": {"PYTHONPATH": "..."}}
//
// A second start (Finder, Dock, Spotlight) reaches the running app as a reopen
// event; it asks the viewer to bring its window to the front.

import AppKit

final class Launcher: NSObject, NSApplicationDelegate {
    private var viewer: Process?

    private func config() -> [String: Any]? {
        let file = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/amigent/app.json")
        guard let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data),
              let config = object as? [String: Any] else { return nil }
        return config
    }

    private func start(_ extra: [String]) -> Process? {
        guard let config = config(), let python = config["python"] as? String else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "amigent.viewer"] + (config["args"] as? [String] ?? []) + extra
        var environment = ProcessInfo.processInfo.environment
        for (key, value) in config["env"] as? [String: String] ?? [:] {
            environment[key] = value
        }
        process.environment = environment
        do {
            try process.run()
        } catch {
            return nil
        }
        return process
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let process = start(["--exit-when-closed"]) else {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Amigent is not set up on this Mac yet"
            alert.informativeText = "Run  amigent-viewer --install  once in Terminal, then start Amigent again."
            alert.runModal()
            NSApp.terminate(nil)
            return
        }
        viewer = process
        process.terminationHandler = { _ in
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        _ = start([])  // finds the running viewer and brings its window up
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let viewer = viewer, viewer.isRunning {
            viewer.terminate()
        }
    }
}

let app = NSApplication.shared
let launcher = Launcher()
app.delegate = launcher
app.setActivationPolicy(.accessory)
app.run()

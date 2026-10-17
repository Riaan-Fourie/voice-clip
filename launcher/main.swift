// VoiceClip Launcher — runs VoiceClip with no Terminal window (Jarvis #407).
//
// CGEventTap needs Accessibility and the recorder needs the Microphone. macOS
// checks both against the RESPONSIBLE process: the app that started the process
// tree. Started from Terminal, that is Terminal, which is why VoiceClip used to
// need a Terminal window. Started by launchd, a process is its own responsible
// process, so a bare `python main.py` from a LaunchAgent has no permissions and
// its tap is silently disabled (GOTCHAS.md).
//
// This launcher is the fix: a tiny signed app bundle that holds the two grants
// itself. launchd starts it, it posix_spawns python as a CHILD, and the child
// inherits the launcher's responsibility, exactly as it used to inherit Terminal's.
//
// Two rules this file must keep:
//
//   1. Spawn and wait, NEVER exec. An exec would replace this image with the Python
//      trampoline, and the responsible identity would become Python.app, which
//      holds no grant. The launcher stays alive as the parent for the whole run.
//   2. Don't rebuild casually. The bundle is ad-hoc signed, so its TCC grant is
//      pinned to this exact binary's hash. A rebuilt binary is a stranger to TCC,
//      and Accessibility has to be switched on again by hand.

import AVFoundation
import ApplicationServices
import Darwin
import Foundation

func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write("\(stamp) [launcher] \(message)\n".data(using: .utf8)!)
}

guard let repo = Bundle.main.object(forInfoDictionaryKey: "VCRepoDir") as? String else {
    log("no VCRepoDir in Info.plist; rebuild with scripts/install-launcher.sh")
    exit(78)  // EX_CONFIG
}

// --- 1. Accessibility --------------------------------------------------------
// The prompt option adds this app to the Accessibility list (switched off) and
// shows the system dialog, so the only manual step is flipping the switch.
// macOS caches the answer for the life of the process, so this loop does NOT see
// the switch go on. The next start does: install-launcher.sh says to kickstart,
// and the watchdog kickstarts within 10 minutes anyway.
let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
if !AXIsProcessTrustedWithOptions([promptKey: true] as CFDictionary) {
    log("waiting for Accessibility: System Settings > Privacy & Security > Accessibility > VoiceClip Launcher")
    while !AXIsProcessTrusted() { sleep(2) }
}
log("accessibility granted")

// --- 2. Microphone -----------------------------------------------------------
// Ask before python starts, so the one-time prompt never lands mid-dictation.
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    break
case .notDetermined:
    log("requesting microphone access")
    let answered = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        log(granted ? "microphone granted" : "microphone DENIED")
        answered.signal()
    }
    answered.wait()
default:
    log("microphone DENIED: System Settings > Privacy & Security > Microphone > VoiceClip Launcher")
}
log("permissions ready")

// --- 3. Run VoiceClip as a child ---------------------------------------------
guard FileManager.default.changeCurrentDirectoryPath(repo) else {
    log("cannot cd to \(repo)")
    exit(78)
}

var child: pid_t = 0
let argv = ["./venv/bin/python", "main.py"]
var cArgs: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0) } + [nil]
let rc = posix_spawn(&child, argv[0], nil, nil, &cArgs, environ)
guard rc == 0 else {
    log("posix_spawn failed: \(String(cString: strerror(rc)))")
    exit(71)  // EX_OSERR
}
log("started VoiceClip pid \(child)")

// launchd stops a job by signalling the launcher; pass it on so python can exit.
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig) { received in kill(child, received) }
}

var status: Int32 = 0
while waitpid(child, &status, 0) == -1 && errno == EINTR {}

// Mirror the child's exit so launchd's KeepAlive sees what python did: a clean
// quit (or a duplicate instance) is 0 and stays down, a crash or kill restarts.
let code: Int32
if status & 0x7f == 0 {
    code = (status >> 8) & 0xff
} else {
    code = 128 + (status & 0x7f)
}
log("VoiceClip pid \(child) ended, exit \(code)")
exit(code)

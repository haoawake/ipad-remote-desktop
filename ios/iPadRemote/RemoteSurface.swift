import SwiftUI
import UIKit

private enum KeyMap {
    static func code(_ raw: UInt32) -> String? {
        if (0x04...0x1d).contains(raw) {
            return "Key" + String(UnicodeScalar(Int(raw - 0x04) + 65)!)
        }
        if (0x1e...0x26).contains(raw) { return "Digit\(raw - 0x1e + 1)" }
        if raw == 0x27 { return "Digit0" }
        if (0x3a...0x45).contains(raw) { return "F\(raw - 0x3a + 1)" }
        if (0x68...0x73).contains(raw) { return "F\(raw - 0x68 + 13)" }
        if (0x59...0x61).contains(raw) { return "Numpad\(raw - 0x59 + 1)" }
        if raw == 0x62 { return "Numpad0" }
        return [
            0x28: "Enter", 0x29: "Escape", 0x2a: "Backspace", 0x2b: "Tab", 0x2c: "Space",
            0x2d: "Minus", 0x2e: "Equal", 0x2f: "BracketLeft", 0x30: "BracketRight",
            0x31: "Backslash", 0x33: "Semicolon", 0x34: "Quote", 0x35: "Backquote",
            0x36: "Comma", 0x37: "Period", 0x38: "Slash", 0x39: "CapsLock",
            0x46: "PrintScreen", 0x47: "ScrollLock", 0x48: "Pause",
            0x49: "Insert", 0x4a: "Home", 0x4b: "PageUp", 0x4c: "Delete",
            0x4d: "End", 0x4e: "PageDown", 0x4f: "ArrowRight", 0x50: "ArrowLeft",
            0x51: "ArrowDown", 0x52: "ArrowUp", 0x53: "NumLock",
            0x54: "NumpadDivide", 0x55: "NumpadMultiply", 0x56: "NumpadSubtract",
            0x57: "NumpadAdd", 0x58: "NumpadEnter", 0x63: "NumpadDecimal",
            0x64: "IntlBackslash", 0x65: "ContextMenu",
            0xe0: "ControlLeft", 0xe1: "ShiftLeft", 0xe2: "AltLeft", 0xe3: "MetaLeft",
            0xe4: "ControlRight", 0xe5: "ShiftRight", 0xe6: "AltRight", 0xe7: "MetaRight"
        ][raw]
    }

    static func remap(_ code: String, os: String) -> String {
        if os != "mac" && code.hasPrefix("Meta") {
            return code == "MetaRight" ? "ControlRight" : "ControlLeft"
        }
        return code
    }

    static func mods(_ flags: UIKeyModifierFlags, os: String) -> [String] {
        var result: [String] = []
        if flags.contains(.control) { result.append("ControlLeft") }
        if flags.contains(.alternate) { result.append("AltLeft") }
        if flags.contains(.shift) { result.append("ShiftLeft") }
        if flags.contains(.command) { result.append(remap("MetaLeft", os: os)) }
        return Array(Set(result)).sorted()
    }

    static func commandCode(_ input: String) -> String? {
        if input.unicodeScalars.count == 1, let c = input.uppercased().unicodeScalars.first {
            if (65...90).contains(c.value) { return "Key" + String(c) }
            if (48...57).contains(c.value) { return "Digit" + String(c) }
        }
        switch input {
        case UIKeyCommand.inputUpArrow: return "ArrowUp"
        case UIKeyCommand.inputDownArrow: return "ArrowDown"
        case UIKeyCommand.inputLeftArrow: return "ArrowLeft"
        case UIKeyCommand.inputRightArrow: return "ArrowRight"
        case "\t": return "Tab"
        case " ": return "Space"
        default: return nil
        }
    }
}

@MainActor
final class KeyForwarder {
    weak var remote: RemoteSession?
    private var held: Set<String> = []
    private static let modifiers: Set<String> = [
        "ControlLeft", "ControlRight", "AltLeft", "AltRight",
        "ShiftLeft", "ShiftRight", "MetaLeft", "MetaRight"
    ]

    init(_ remote: RemoteSession) { self.remote = remote }

    func physical(_ key: UIKey, isDown: Bool) {
        guard let remote, remote.connected else { return }
        let raw = UInt32(key.keyCode.rawValue)
        guard let physicalCode = KeyMap.code(raw) else {
            if isDown, KeyMap.mods(key.modifierFlags, os: remote.os).isEmpty,
               !key.characters.isEmpty {
                remote.send(["t": "text", "s": key.characters])
            }
            return
        }
        let code = KeyMap.remap(physicalCode, os: remote.os)
        if !Self.modifiers.contains(physicalCode) {
            sync(key.modifierFlags, remote: remote)
        }
        set(code, down: isDown, remote: remote)
    }

    func command(_ input: String, modifiers: UIKeyModifierFlags) {
        guard let remote, remote.connected, let code = KeyMap.commandCode(input) else { return }
        // Key commands have no key-up; send an atomic shortcut to desktop.
        remote.send(["t": "combo", "codes": KeyMap.mods(modifiers, os: remote.os) + [code]])
    }

    private func sync(_ flags: UIKeyModifierFlags, remote: RemoteSession) {
        let expected = Set(KeyMap.mods(flags, os: remote.os))
        for code in held.intersection(Self.modifiers).subtracting(expected) {
            set(code, down: false, remote: remote)
        }
        for code in expected.subtracting(held) {
            set(code, down: true, remote: remote)
        }
    }

    private func set(_ code: String, down: Bool, remote: RemoteSession) {
        if down {
            if held.insert(code).inserted {
                remote.send(["t": "key", "code": code, "d": true])
            }
        } else if held.remove(code) != nil {
            remote.send(["t": "key", "code": code, "d": false])
        }
    }

    func release() {
        if let remote, remote.connected { remote.send(["t": "release"]) }
        held.removeAll()
    }
}

struct RemoteSurface: UIViewControllerRepresentable {
    @ObservedObject var remote: RemoteSession

    func makeUIViewController(context: Context) -> SurfaceController {
        SurfaceController(remote: remote)
    }

    func updateUIViewController(_ controller: SurfaceController, context: Context) {
        controller.update(remote: remote)
    }
}

@MainActor
final class SurfaceController: UIViewController {
    private let remote: RemoteSession
    private let keyboard: KeyForwarder
    private let canvas = KeyboardCanvas()
    private var didFocus = false

    init(remote: RemoteSession) {
        self.remote = remote
        self.keyboard = KeyForwarder(remote)
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("Use init(remote:)") }

    override func loadView() {
        canvas.remote = remote
        canvas.keyboard = keyboard
        view = canvas
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        focus()
    }

    func update(remote: RemoteSession) {
        canvas.display(remote.image, geometry: remote.geom)
        if remote.connected && !didFocus {
            didFocus = true
            DispatchQueue.main.async { [weak self] in self?.focus() }
        }
    }

    private func focus() {
        guard remote.connected, isViewLoaded, canvas.window != nil else { return }
        _ = canvas.becomeFirstResponder()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        keyboard.release()
    }
}

@MainActor
private final class KeyboardCanvas: UIView {
    weak var remote: RemoteSession?
    var keyboard: KeyForwarder?
    private let picture = UIImageView()
    private var geometry: Geometry?
    private var zoom: CGFloat = 1

    override var canBecomeFirstResponder: Bool { true }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        clipsToBounds = true
        picture.contentMode = .scaleToFill
        picture.backgroundColor = .black
        picture.isUserInteractionEnabled = false
        addSubview(picture)
        NotificationCenter.default.addObserver(self, selector: #selector(releaseOnBackground),
                                               name: UIApplication.willResignActiveNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(refocusOnActive),
                                               name: UIApplication.didBecomeActiveNotification, object: nil)

        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped(_:)))
        let twice = UITapGestureRecognizer(target: self, action: #selector(doubleTapped(_:)))
        twice.numberOfTapsRequired = 2
        tap.require(toFail: twice)
        addGestureRecognizer(tap)
        addGestureRecognizer(twice)

        let rightClick = UITapGestureRecognizer(target: self, action: #selector(rightTapped(_:)))
        rightClick.numberOfTouchesRequired = 2
        addGestureRecognizer(rightClick)

        let drag = UIPanGestureRecognizer(target: self, action: #selector(dragged(_:)))
        drag.minimumNumberOfTouches = 1
        drag.maximumNumberOfTouches = 1
        addGestureRecognizer(drag)

        let scroll = UIPanGestureRecognizer(target: self, action: #selector(scrolled(_:)))
        scroll.minimumNumberOfTouches = 2
        scroll.maximumNumberOfTouches = 2
        addGestureRecognizer(scroll)

        let pinch = UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:)))
        addGestureRecognizer(pinch)

        let hover = UIHoverGestureRecognizer(target: self, action: #selector(hovered(_:)))
        addGestureRecognizer(hover)
    }

    required init?(coder: NSCoder) { fatalError("Use init(frame:)") }

    @objc private func releaseOnBackground() {
        keyboard?.release()
    }

    @objc private func refocusOnActive() {
        if remote?.connected == true { _ = becomeFirstResponder() }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        guard let geometry, geometry.sw > 0, geometry.sh > 0 else { return }
        let factor = min(bounds.width / CGFloat(geometry.sw), bounds.height / CGFloat(geometry.sh)) * zoom
        let size = CGSize(width: CGFloat(geometry.sw) * factor, height: CGFloat(geometry.sh) * factor)
        picture.frame = CGRect(x: (bounds.width - size.width) / 2,
                               y: (bounds.height - size.height) / 2,
                               width: size.width, height: size.height)
    }

    func display(_ image: UIImage?, geometry: Geometry?) {
        picture.image = image
        if let geometry {
            let changed = self.geometry.map { $0.sw != geometry.sw || $0.sh != geometry.sh } ?? true
            self.geometry = geometry
            if changed { zoom = 1; setNeedsLayout() }
        }
    }

    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        guard remote?.connected == true else { super.pressesBegan(presses, with: event); return }
        for press in presses { if let key = press.key { keyboard?.physical(key, isDown: true) } }
    }

    override func pressesEnded(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        guard remote?.connected == true else { super.pressesEnded(presses, with: event); return }
        for press in presses { if let key = press.key { keyboard?.physical(key, isDown: false) } }
    }

    override func pressesCancelled(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        for press in presses { if let key = press.key { keyboard?.physical(key, isDown: false) } }
        keyboard?.release()
    }

    override var keyCommands: [UIKeyCommand]? {
        // UIKeyCommand captures actions iPadOS might route through its responder
        // chain instead of delivering to pressesBegan. This does not override OS
        // reserved combinations such as Command+Tab.
        let combos: [UIKeyModifierFlags] = [
            [.command], [.command, .shift], [.command, .alternate],
            [.control], [.control, .shift], [.alternate]
        ]
        let letters = "abcdefghijklmnopqrstuvwxyz"
        var result: [UIKeyCommand] = []
        for modifier in combos {
            for char in letters {
                let cmd = UIKeyCommand(input: String(char), modifierFlags: modifier,
                                       action: #selector(forwardCommand(_:)))
                cmd.wantsPriorityOverSystemBehavior = true
                result.append(cmd)
            }
        }
        return result
    }

    @objc private func forwardCommand(_ cmd: UIKeyCommand) {
        if let input = cmd.input { keyboard?.command(input, modifiers: cmd.modifierFlags) }
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        activate()
        guard let point = pointToRemote(g.location(in: self)) else { return }
        remote?.send(["t": "click", "b": 0, "n": 1, "x": point.x, "y": point.y])
    }

    @objc private func doubleTapped(_ g: UITapGestureRecognizer) {
        activate()
        guard let point = pointToRemote(g.location(in: self)) else { return }
        remote?.send(["t": "click", "b": 0, "n": 2, "x": point.x, "y": point.y])
    }

    @objc private func rightTapped(_ g: UITapGestureRecognizer) {
        activate()
        guard let point = pointToRemote(g.location(in: self)) else { return }
        remote?.send(["t": "click", "b": 2, "n": 1, "x": point.x, "y": point.y])
    }

    @objc private func dragged(_ g: UIPanGestureRecognizer) {
        activate()
        guard let p = pointToRemote(g.location(in: self)) else { return }
        switch g.state {
        case .began:
            remote?.send(["t": "mb", "b": 0, "d": true, "x": p.x, "y": p.y])
        case .changed:
            remote?.send(["t": "mm", "x": p.x, "y": p.y])
        case .ended, .failed, .cancelled:
            remote?.send(["t": "mb", "b": 0, "d": false, "x": p.x, "y": p.y])
        default: break
        }
    }

    @objc private func scrolled(_ g: UIPanGestureRecognizer) {
        activate()
        if g.state == .changed {
            let change = g.translation(in: self)
            g.setTranslation(.zero, in: self)
            remote?.send(["t": "wh", "dx": Int(change.x * 2), "dy": Int(change.y * 2)])
        }
    }

    @objc private func pinched(_ g: UIPinchGestureRecognizer) {
        if g.state == .began || g.state == .changed {
            zoom = min(5, max(1, zoom * g.scale))
            g.scale = 1
            setNeedsLayout()
        }
    }

    @objc private func hovered(_ g: UIHoverGestureRecognizer) {
        guard g.state == .began || g.state == .changed,
              let p = pointToRemote(g.location(in: self)) else { return }
        remote?.send(["t": "mm", "x": p.x, "y": p.y])
    }

    private func activate() { if !isFirstResponder { _ = becomeFirstResponder() } }

    private func pointToRemote(_ p: CGPoint) -> (x: Int, y: Int)? {
        guard let g = geometry, picture.bounds.width > 0, picture.bounds.height > 0 else { return nil }
        let u = max(0, min(1, (p.x - picture.frame.minX) / picture.frame.width))
        let v = max(0, min(1, (p.y - picture.frame.minY) / picture.frame.height))
        return (Int(u * CGFloat(max(0, g.width - 1))), Int(v * CGFloat(max(0, g.height - 1))))
    }
}

import SwiftUI
import ImageIO

struct Geometry {
    let width: Int
    let height: Int
    let sw: Int
    let sh: Int
}

actor VideoBuffer {
    private var context: CGContext?
    private var width = 0
    private var height = 0

    func reset(width: Int, height: Int) {
        guard (1...16384).contains(width), (1...16384).contains(height) else { return }
        self.width = width
        self.height = height
        context = CGContext(data: nil, width: width, height: height,
                            bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue)
        context?.interpolationQuality = .low
    }

    func decode(_ data: Data) -> (UInt32, UIImage?)? {
        let b = [UInt8](data)
        guard b.count >= 7, b[0] == 1 else { return nil }
        func u16(_ i: Int) -> Int { Int(b[i]) | (Int(b[i + 1]) << 8) }
        func u32(_ i: Int) -> UInt32 {
            UInt32(b[i]) | (UInt32(b[i + 1]) << 8) |
                (UInt32(b[i + 2]) << 16) | (UInt32(b[i + 3]) << 24)
        }
        let id = u32(1), count = u16(5)
        guard count <= 1024 else { return (id, nil) }
        var offset = 7
        for _ in 0..<count {
            guard offset + 12 <= b.count else { return (id, nil) }
            let x = u16(offset), y = u16(offset + 2)
            let w = u16(offset + 4), h = u16(offset + 6)
            let length = Int(u32(offset + 8))
            offset += 12
            guard length > 0, length <= 8_000_000, offset + length <= b.count else { return (id, nil) }
            if x + w <= width, y + h <= height, w > 0, h > 0,
               let source = CGImageSourceCreateWithData(data.subdata(in: offset..<(offset + length)) as CFData, nil),
               let picture = CGImageSourceCreateImageAtIndex(source, 0, nil) {
                context?.draw(picture, in: CGRect(x: x, y: height - y - h, width: w, height: h))
            }
            offset += length
        }
        return (id, context?.makeImage().map { UIImage(cgImage: $0) })
    }
}

@MainActor
final class RemoteSession: ObservableObject {
    @Published private(set) var connected = false
    @Published private(set) var connecting = false
    @Published private(set) var error: String?
    @Published private(set) var image: UIImage?
    @Published private(set) var geom: Geometry?
    @Published private(set) var os = "win"
    @Published private(set) var host = ""

    private let http: URLSession
    private let decoder = VideoBuffer()
    private var socket: URLSessionWebSocketTask?
    private var reader: Task<Void, Never>?
    private var queue: [String] = []
    private var sending = false
    private var generation = 0

    init() {
        let config = URLSessionConfiguration.default
        config.httpShouldSetCookies = true
        config.httpCookieStorage = .shared
        http = URLSession(configuration: config)
    }

    func connect(address: String, password: String) async {
        disconnect()
        connecting = true
        let tag = generation
        do {
            let base = try origin(address)
            if !password.isEmpty {
                var request = URLRequest(url: base.appendingPathComponent("api/login"))
                request.httpMethod = "POST"
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONSerialization.data(withJSONObject: ["password": password, "remember": true])
                let (data, response) = try await http.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                    let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                    throw Problem.message(info?["error"] as? String ?? "登录密码错误")
                }
            }
            let (data, response) = try await http.data(from: base.appendingPathComponent("api/me"))
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw Problem.message("请填写电脑上显示的登录密码")
            }
            guard tag == generation else { return }
            let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            os = info?["os"] as? String ?? "win"
            host = info?["host"] as? String ?? ""
            var components = URLComponents(url: base.appendingPathComponent("ws"), resolvingAgainstBaseURL: false)!
            components.scheme = base.scheme == "https" ? "wss" : "ws"
            components.queryItems = [
                URLQueryItem(name: "scale", value: "0.75"),
                URLQueryItem(name: "quality", value: "70"),
                URLQueryItem(name: "fps", value: "30")
            ]
            // The desktop server authenticates the WebSocket with the same
            // HttpOnly session cookie as /api/me. Make it explicit for iOS.
            var request = URLRequest(url: components.url!)
            if let cookies = http.configuration.httpCookieStorage?.cookies(for: base),
               let cookieHeader = HTTPCookie.requestHeaderFields(with: cookies)["Cookie"] {
                request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
            }
            let task = http.webSocketTask(with: request)
            socket = task
            task.resume()
            reader = Task { await receive(task, tag: tag) }
        } catch {
            guard tag == generation else { return }
            self.error = error.localizedDescription
            connecting = false
        }
    }

    private func origin(_ input: String) throws -> URL {
        var value = input.trimmingCharacters(in: .whitespacesAndNewlines)
        if !value.contains("://") { value = "http://" + value }
        guard let c = URLComponents(string: value),
              ["http", "https"].contains(c.scheme?.lowercased() ?? ""),
              c.host != nil else { throw Problem.message("电脑地址格式错误") }
        var result = URLComponents()
        result.scheme = c.scheme
        result.host = c.host
        result.port = c.port
        guard let url = result.url else { throw Problem.message("电脑地址无效") }
        return url
    }

    private func receive(_ task: URLSessionWebSocketTask, tag: Int) async {
        do {
            while !Task.isCancelled {
                let packet = try await task.receive()
                guard tag == generation else { return }
                switch packet {
                case .string(let s): await handle(s)
                case .data(let data):
                    if let (id, rendered) = await decoder.decode(data) {
                        if let rendered { image = rendered }
                        send(["t": "ack", "id": id])
                    }
                @unknown default: break
                }
            }
        } catch {
            guard tag == generation else { return }
            let msg = "远程连接已断开：\(error.localizedDescription)"
            disconnect()
            self.error = msg
        }
    }

    private func handle(_ value: String) async {
        guard let msg = (try? JSONSerialization.jsonObject(with: Data(value.utf8))) as? [String: Any] else { return }
        if msg["t"] as? String == "hello" {
            if let g = msg["geom"] as? [String: Any],
               let w = g["width"] as? Int, let h = g["height"] as? Int,
               let sw = g["sw"] as? Int, let sh = g["sh"] as? Int {
                geom = Geometry(width: w, height: h, sw: sw, sh: sh)
                await decoder.reset(width: sw, height: sh)
            }
            os = msg["os"] as? String ?? os
            host = msg["host"] as? String ?? host
            connecting = false
            connected = true
        }
    }

    func send(_ message: [String: Any]) {
        guard socket != nil,
              let data = try? JSONSerialization.data(withJSONObject: message),
              let text = String(data: data, encoding: .utf8) else { return }
        queue.append(text)
        if !sending {
            sending = true
            Task { await drain() }
        }
    }

    private func drain() async {
        let current = socket
        while let current, current === socket, !queue.isEmpty {
            let text = queue.removeFirst()
            do { try await current.send(.string(text)) }
            catch { break }
        }
        sending = false
    }

    func disconnect() {
        generation += 1
        reader?.cancel()
        reader = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        queue.removeAll()
        sending = false
        image = nil
        geom = nil
        connected = false
        connecting = false
        error = nil
    }

    private enum Problem: LocalizedError {
        case message(String)
        var errorDescription: String? {
            if case .message(let text) = self { return text }
            return nil
        }
    }
}

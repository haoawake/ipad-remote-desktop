import SwiftUI

@main
struct NativeRemoteApp: App {
    @StateObject private var remote = RemoteSession()
    var body: some Scene {
        WindowGroup {
            HomeScreen().environmentObject(remote).preferredColorScheme(.dark)
        }
    }
}

struct HomeScreen: View {
    @EnvironmentObject private var remote: RemoteSession
    @State private var address = UserDefaults.standard.string(forKey: "remoteAddress") ?? ""
    @State private var password = ""
    @State private var showShortcuts = false
    @State private var showText = false
    @State private var written = ""

    var body: some View {
        Group {
            if remote.connected {
                ZStack(alignment: .top) {
                    RemoteSurface(remote: remote).ignoresSafeArea()
                    HStack {
                        Label(remote.host.isEmpty ? "远程电脑" : remote.host, systemImage: "desktopcomputer")
                            .font(.footnote.bold()).lineLimit(1)
                        Spacer()
                        Button("快捷键") { showShortcuts = true }
                        Button("中文输入") { showText = true }
                        Button("断开") { remote.disconnect() }
                    }
                    .buttonStyle(.bordered)
                    .padding(10)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
                    .padding()
                }
                .sheet(isPresented: $showShortcuts) {
                    NavigationStack {
                        List {
                            ForEach(shortcuts, id: \.name) { item in
                                Button(item.name) {
                                    remote.send(["t": "combo", "codes": item.codes])
                                    showShortcuts = false
                                }
                            }
                        }
                        .navigationTitle("发送远程快捷键")
                        .toolbar { Button("关闭") { showShortcuts = false } }
                    }
                    .presentationDetents([.medium, .large])
                }
                .sheet(isPresented: $showText) {
                    NavigationStack {
                        Form {
                            TextEditor(text: $written).frame(minHeight: 110)
                            Button("发送文字到远程电脑") {
                                if !written.isEmpty { remote.send(["t": "text", "s": written]) }
                                written = ""
                                showText = false
                            }
                        }
                        .navigationTitle("中文和特殊文字")
                        .toolbar { Button("取消") { showText = false } }
                    }.presentationDetents([.medium])
                }
            } else {
                VStack(spacing: 22) {
                    Spacer()
                    Image(systemName: "desktopcomputer.and.ipad")
                        .font(.system(size: 56)).foregroundStyle(.cyan)
                    Text("iPad 远程桌面").font(.largeTitle.bold())
                    Text("原生屏幕控制 · 实体键盘直通 Windows / Mac")
                        .foregroundStyle(.secondary)
                    VStack(spacing: 14) {
                        TextField("电脑地址：http://100.x.y.z:8765", text: $address)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                        SecureField("登录密码（已登录过可留空）", text: $password)
                            .onSubmit(connect)
                        Button(remote.connecting ? "连接中…" : "连接电脑", action: connect)
                            .buttonStyle(.borderedProminent)
                            .disabled(remote.connecting || address.trimmingCharacters(in: .whitespaces).isEmpty)
                        if let error = remote.error {
                            Text(error).foregroundStyle(.red).font(.footnote)
                        }
                    }
                    .textFieldStyle(.roundedBorder)
                    .padding(24)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
                    .frame(maxWidth: 480)
                    Text("连接后自动接管 App 可收到的实体键盘事件；系统保留的 ⌘Tab 等键仍归 iPadOS。")
                        .font(.footnote).multilineTextAlignment(.center).foregroundStyle(.secondary)
                    Spacer()
                }.padding().background(Color(red: 0.04, green: 0.06, blue: 0.10))
            }
        }
    }

    private var shortcuts: [(name: String, codes: [String])] {
        if remote.os == "mac" {
            return [
                ("⌘Tab 切换应用", ["MetaLeft", "Tab"]),
                ("⌘空格 Spotlight", ["MetaLeft", "Space"]),
                ("⌘C 复制", ["MetaLeft", "KeyC"]),
                ("⌘V 粘贴", ["MetaLeft", "KeyV"]),
                ("⌘Z 撤销", ["MetaLeft", "KeyZ"]),
                ("⌘Q 退出", ["MetaLeft", "KeyQ"])
            ]
        }
        return [
            ("Alt+Tab 切换窗口", ["AltLeft", "Tab"]),
            ("Win+D 显示桌面", ["MetaLeft", "KeyD"]),
            ("Ctrl+C 复制", ["ControlLeft", "KeyC"]),
            ("Ctrl+V 粘贴", ["ControlLeft", "KeyV"]),
            ("Ctrl+Z 撤销", ["ControlLeft", "KeyZ"]),
            ("Ctrl+Shift+Esc 任务管理器", ["ControlLeft", "ShiftLeft", "Escape"])
        ]
    }

    private func connect() {
        UserDefaults.standard.set(address, forKey: "remoteAddress")
        let pass = password
        password = ""
        Task { await remote.connect(address: address, password: pass) }
    }
}

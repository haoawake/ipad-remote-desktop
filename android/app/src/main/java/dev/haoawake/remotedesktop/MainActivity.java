package dev.haoawake.remotedesktop;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.text.InputType;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** True Android Activity: no WebView, no browser, native keyboard and touch. */
public final class MainActivity extends Activity implements RemoteClient.Listener {
    private static final int BG = Color.rgb(12, 17, 27);
    private static final int PANEL = Color.rgb(30, 38, 55);
    private static final int TEXT = Color.rgb(234, 240, 252);
    private static final int MUTED = Color.rgb(155, 170, 192);
    private static final int ACCENT = Color.rgb(79, 172, 255);

    private RemoteClient remote;
    private FrameLayout root;
    private FrameLayout sessionPane;
    private RemoteSurface screen;
    private EditText addressInput, passwordInput;
    private TextView status;
    private boolean typing;
    private boolean trackpad;
    private boolean toolbarVisible = true;

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(BG);
        getWindow().setNavigationBarColor(BG);
        remote = new RemoteClient(this);
        root = new FrameLayout(this);
        root.setBackgroundColor(BG);
        setContentView(root);
        showLogin();
    }

    private int dp(float v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private LinearLayout vertical() {
        LinearLayout v = new LinearLayout(this);
        v.setOrientation(LinearLayout.VERTICAL);
        return v;
    }

    private TextView label(String message, int size, int color) {
        TextView v = new TextView(this);
        v.setText(message);
        v.setTextColor(color);
        v.setTextSize(size);
        return v;
    }

    private Button button(String title, View.OnClickListener action) {
        Button b = new Button(this);
        b.setText(title);
        b.setTextSize(13);
        b.setTextColor(TEXT);
        b.setAllCaps(false);
        b.setBackgroundTintList(android.content.res.ColorStateList.valueOf(PANEL));
        b.setOnClickListener(action);
        return b;
    }

    private void showLogin() {
        if (isFinishing()) return;
        typing = false;
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().getDecorView().setSystemUiVisibility(0);
        root.removeAllViews();
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout wrap = vertical();
        wrap.setGravity(Gravity.CENTER);
        wrap.setPadding(dp(22), dp(30), dp(22), dp(30));
        scroll.addView(wrap, new ScrollView.LayoutParams(-1, -1));

        TextView icon = label("▣", 64, ACCENT);
        icon.setGravity(Gravity.CENTER);
        wrap.addView(icon, new LinearLayout.LayoutParams(-1, dp(82)));
        TextView title = label("远程桌面", 29, TEXT);
        title.setGravity(Gravity.CENTER);
        title.setTypeface(null, 1);
        wrap.addView(title, new LinearLayout.LayoutParams(-1, -2));

        TextView desc = label("Android 原生客户端 · 操控 Windows / Mac", 14, MUTED);
        desc.setGravity(Gravity.CENTER);
        desc.setPadding(0, dp(10), 0, dp(22));
        wrap.addView(desc);

        LinearLayout card = vertical();
        card.setPadding(dp(20), dp(22), dp(20), dp(22));
        card.setBackgroundColor(PANEL);
        LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(-1, -2);
        cardParams.bottomMargin = dp(14);
        wrap.addView(card, cardParams);

        card.addView(label("远程电脑地址", 14, TEXT));
        addressInput = new EditText(this);
        addressInput.setSingleLine(true);
        addressInput.setTextColor(TEXT);
        addressInput.setHintTextColor(MUTED);
        addressInput.setText(getPreferences(MODE_PRIVATE).getString("address", ""));
        addressInput.setHint("http://100.x.x.x  或  https://…");
        addressInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        card.addView(addressInput, new LinearLayout.LayoutParams(-1, dp(55)));

        TextView passLabel = label("远程登录密码", 14, TEXT);
        passLabel.setPadding(0, dp(12), 0, 0);
        card.addView(passLabel);
        passwordInput = new EditText(this);
        passwordInput.setTextColor(TEXT);
        passwordInput.setHintTextColor(MUTED);
        passwordInput.setSingleLine(true);
        passwordInput.setHint("电脑端显示的密码");
        passwordInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        card.addView(passwordInput, new LinearLayout.LayoutParams(-1, dp(55)));

        Button connect = button("连 接 电 脑", v -> {
            String address = addressInput.getText().toString().trim();
            if (address.isEmpty()) { addressInput.setError("请填写电脑的访问地址"); return; }
            getPreferences(MODE_PRIVATE).edit().putString("address", address).apply();
            String password = passwordInput.getText().toString();
            passwordInput.setText("");
            remote.connect(address, password);
        });
        connect.setBackgroundTintList(android.content.res.ColorStateList.valueOf(ACCENT));
        connect.setTextColor(Color.BLACK);
        LinearLayout.LayoutParams connectParams = new LinearLayout.LayoutParams(-1, dp(54));
        connectParams.topMargin = dp(16);
        card.addView(connect, connectParams);

        status = label("先在手机安装并连接 Tailscale，再连接电脑。\n密码只用于登录，不会保存在手机中。", 13, MUTED);
        status.setGravity(Gravity.CENTER);
        wrap.addView(status);

        TextView footer = label("支持蓝牙/USB 实体键盘、鼠标、触控手势及常用快捷键。\nAndroid 系统级 Home / 多任务组合键仍归系统控制。", 12, MUTED);
        footer.setGravity(Gravity.CENTER);
        footer.setPadding(0, dp(24), 0, 0);
        wrap.addView(footer);
        root.addView(scroll, new FrameLayout.LayoutParams(-1, -1));
    }

    private void showRemote() {
        if (sessionPane != null && sessionPane.getParent() == root) return;
        root.removeAllViews();
        sessionPane = new FrameLayout(this);
        root.addView(sessionPane, new FrameLayout.LayoutParams(-1, -1));
        screen = new RemoteSurface(this, remote);
        sessionPane.addView(screen, new FrameLayout.LayoutParams(-1, -1));
        screen.requestFocus();

        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setPadding(dp(7), dp(4), dp(7), dp(4));
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setBackgroundColor(Color.argb(224, 26, 35, 50));

        Button mode = button("直点", v -> {
            trackpad = !trackpad;
            screen.setTrackpad(trackpad);
            ((Button) v).setText(trackpad ? "触控板" : "直点");
            screen.requestFocus();
        });
        toolbar.addView(mode, new LinearLayout.LayoutParams(0, dp(48), 1));

        Button shortcut = button("快捷键", v -> shortcutDialog());
        toolbar.addView(shortcut, new LinearLayout.LayoutParams(0, dp(48), 1));

        Button input = button("文字", v -> textDialog());
        toolbar.addView(input, new LinearLayout.LayoutParams(0, dp(48), 1));

        Button scale = button("重置", v -> {
            screen.resetZoom();
            screen.requestFocus();
        });
        toolbar.addView(scale, new LinearLayout.LayoutParams(0, dp(48), 1));

        Button disconnect = button("断开", v -> remote.disconnect());
        toolbar.addView(disconnect, new LinearLayout.LayoutParams(0, dp(48), 1));

        FrameLayout.LayoutParams barParams = new FrameLayout.LayoutParams(-1, dp(56), Gravity.TOP);
        sessionPane.addView(toolbar, barParams);

        Button show = button("☰", v -> {
            toolbarVisible = !toolbarVisible;
            toolbar.setVisibility(toolbarVisible ? View.VISIBLE : View.GONE);
            screen.requestFocus();
        });
        FrameLayout.LayoutParams toggleParams = new FrameLayout.LayoutParams(dp(54), dp(42), Gravity.TOP | Gravity.RIGHT);
        toggleParams.topMargin = dp(56);
        sessionPane.addView(show, toggleParams);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        screen.post(screen::requestFocus);
    }

    private void shortcutDialog() {
        typing = true;
        final String[] names;
        final List<List<String>> combos = new ArrayList<>();
        if (remote.mac) {
            names = new String[] {"⌘ Tab · 切换应用", "⌘ 空格 · Spotlight", "⌘ C · 复制",
                    "⌘ V · 粘贴", "⌘ A · 全选", "⌘ Z · 撤销", "⌘ Q · 退出",
                    "Control + ↑ · Mission Control"};
            combos.add(RemoteKeys.codes("MetaLeft", "Tab"));
            combos.add(RemoteKeys.codes("MetaLeft", "Space"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyC"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyV"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyA"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyZ"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyQ"));
            combos.add(RemoteKeys.codes("ControlLeft", "ArrowUp"));
        } else {
            names = new String[] {"Alt + Tab · 切换窗口", "Win + D · 显示桌面",
                    "Ctrl + C · 复制", "Ctrl + V · 粘贴", "Ctrl + A · 全选",
                    "Ctrl + Z · 撤销", "Ctrl + Shift + Esc · 任务管理器",
                    "Win + E · 文件资源管理器", "Alt + F4 · 关闭窗口"};
            combos.add(RemoteKeys.codes("AltLeft", "Tab"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyD"));
            combos.add(RemoteKeys.codes("ControlLeft", "KeyC"));
            combos.add(RemoteKeys.codes("ControlLeft", "KeyV"));
            combos.add(RemoteKeys.codes("ControlLeft", "KeyA"));
            combos.add(RemoteKeys.codes("ControlLeft", "KeyZ"));
            combos.add(RemoteKeys.codes("ControlLeft", "ShiftLeft", "Escape"));
            combos.add(RemoteKeys.codes("MetaLeft", "KeyE"));
            combos.add(RemoteKeys.codes("AltLeft", "F4"));
        }
        new AlertDialog.Builder(this)
                .setTitle("发送电脑快捷键")
                .setItems(names, (dialog, item) -> remote.sendCombo(combos.get(item)))
                .setNegativeButton("关闭", null)
                .setOnDismissListener(dialog -> { typing = false; focusRemote(); })
                .show();
    }

    private void textDialog() {
        typing = true;
        EditText input = new EditText(this);
        input.setHint("中文、英文或长文本");
        input.setMinLines(3);
        input.setGravity(Gravity.TOP);
        input.setPadding(dp(18), dp(14), dp(18), dp(14));
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        new AlertDialog.Builder(this)
                .setTitle("发送文本到电脑")
                .setView(input)
                .setPositiveButton("发送", (dialog, which) ->
                        remote.sendText(input.getText().toString()))
                .setNegativeButton("取消", null)
                .setOnDismissListener(dialog -> { typing = false; focusRemote(); })
                .show();
        input.requestFocus();
    }

    private void focusRemote() {
        if (screen != null) screen.post(screen::requestFocus);
    }

    @Override public void onConnection(boolean connected, String message) {
        if (connected) {
            if (sessionPane == null || sessionPane.getParent() != root) showRemote();
            else if (message != null && message.contains("锁屏"))
                Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        } else if (!isFinishing()) {
            if (screen != null && screen.getParent() != null) {
                showLogin();
                sessionPane = null;
                screen = null;
            }
            if (status != null && message != null) status.setText(message);
        }
    }

    @Override public void onFrame() {
        if (screen != null) screen.invalidate();
    }

    @Override public boolean dispatchKeyEvent(KeyEvent event) {
        if (remote != null && remote.connected && !typing && screen != null) {
            int key = event.getKeyCode();
            String code = RemoteKeys.code(key, remote.mac);
            if (code != null && (event.getAction() == KeyEvent.ACTION_DOWN ||
                    event.getAction() == KeyEvent.ACTION_UP)) {
                remote.send("key", code, event.getAction() == KeyEvent.ACTION_DOWN);
                return true;
            }
            if (event.getAction() == KeyEvent.ACTION_DOWN && event.getUnicodeChar() > 31
                    && !event.isCtrlPressed() && !event.isAltPressed() && !event.isMetaPressed()) {
                remote.sendText(new String(Character.toChars(event.getUnicodeChar())));
                return true;
            }
        }
        return super.dispatchKeyEvent(event);
    }

    @Override protected void onPause() {
        if (remote != null) remote.release();
        super.onPause();
    }

    @Override protected void onResume() {
        super.onResume();
        focusRemote();
    }

    @Override protected void onDestroy() {
        if (remote != null) remote.shutdown();
        super.onDestroy();
    }
}

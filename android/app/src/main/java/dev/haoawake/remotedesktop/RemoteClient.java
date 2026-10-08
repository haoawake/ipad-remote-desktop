package dev.haoawake.remotedesktop;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.Cookie;
import okhttp3.CookieJar;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * Native Android transport for the existing Python remote-desktop server.
 * Auth: POST /api/login -> session cookie -> GET /api/me -> WebSocket /ws.
 * Frames: dirty-tile JPEG with ACK, exactly the same protocol as iPad Safari.
 */
final class RemoteClient {
    interface Listener {
        void onConnection(boolean connected, String message);
        void onFrame();
    }

    static final class Geometry {
        final int width, height, sw, sh;
        Geometry(int width, int height, int sw, int sh) {
            this.width = width; this.height = height; this.sw = sw; this.sh = sh;
        }
    }

    final Object frameLock = new Object();
    Bitmap frame;
    volatile Geometry geometry;
    volatile boolean connected;
    volatile boolean mac;
    volatile String host = "";

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Listener listener;
    private final OkHttpClient client;
    private final MemoryCookies cookies = new MemoryCookies();
    private volatile WebSocket socket;
    private volatile int generation;

    RemoteClient(Listener listener) {
        this.listener = listener;
        client = new OkHttpClient.Builder()
                .cookieJar(cookies)
                .connectTimeout(12, java.util.concurrent.TimeUnit.SECONDS)
                .readTimeout(20, java.util.concurrent.TimeUnit.SECONDS)
                .build();
    }

    private static final class MemoryCookies implements CookieJar {
        private final ArrayList<Cookie> values = new ArrayList<>();
        @Override public synchronized void saveFromResponse(HttpUrl url, List<Cookie> next) {
            for (Cookie cookie : next) {
                Iterator<Cookie> it = values.iterator();
                while (it.hasNext()) {
                    Cookie prev = it.next();
                    if (prev.name().equals(cookie.name()) && prev.domain().equals(cookie.domain())
                            && prev.path().equals(cookie.path())) it.remove();
                }
                values.add(cookie);
            }
        }
        @Override public synchronized List<Cookie> loadForRequest(HttpUrl url) {
            ArrayList<Cookie> matches = new ArrayList<>();
            long now = System.currentTimeMillis();
            Iterator<Cookie> it = values.iterator();
            while (it.hasNext()) {
                Cookie cookie = it.next();
                if (cookie.expiresAt() <= now) it.remove();
                else if (cookie.matches(url)) matches.add(cookie);
            }
            return matches;
        }
        synchronized void clear() { values.clear(); }
    }

    void connect(String enteredAddress, String password) {
        disconnect();
        int epoch = generation;
        publish(false, "正在连接电脑…");
        io.execute(() -> {
            try {
                String urlString = enteredAddress.trim();
                if (!urlString.contains("://")) urlString = "http://" + urlString;
                HttpUrl parsed = HttpUrl.parse(urlString);
                if (parsed == null || !("http".equals(parsed.scheme()) || "https".equals(parsed.scheme())))
                    throw new IOException("电脑地址无效，填写 http://100.x.x.x 或 HTTPS 地址");
                HttpUrl base = parsed.newBuilder().encodedPath("/").query(null).fragment(null).build();

                if (!password.isEmpty()) {
                    JSONObject body = new JSONObject();
                    body.put("password", password);
                    body.put("remember", true);
                    Request request = new Request.Builder().url(base.resolve("api/login"))
                            .post(RequestBody.create(MediaType.parse("application/json"),
                                    body.toString())).build();
                    try (Response response = client.newCall(request).execute()) {
                        if (!response.isSuccessful()) {
                            throw new IOException("登录失败（" + response.code() + "），请检查电脑端密码");
                        }
                    }
                }

                Request status = new Request.Builder().url(base.resolve("api/me")).get().build();
                try (Response response = client.newCall(status).execute()) {
                    if (!response.isSuccessful())
                        throw new IOException("未登录，请填写电脑端的登录密码");
                    JSONObject info = new JSONObject(response.body().string());
                    mac = "mac".equals(info.optString("os"));
                    host = info.optString("host");
                }

                if (epoch != generation) return;
                String scheme = base.isHttps() ? "wss://" : "ws://";
                String wsUrl = scheme + base.host() + ":" + base.port()
                        + "/ws?scale=0.75&quality=70&fps=30";

                // Supply the same HTTP session cookie on the WS handshake.
                StringBuilder cookieHeader = new StringBuilder();
                for (Cookie cookie : cookies.loadForRequest(base)) {
                    if (cookieHeader.length() > 0) cookieHeader.append("; ");
                    cookieHeader.append(cookie.name()).append("=").append(cookie.value());
                }
                Request.Builder wsRequest = new Request.Builder().url(wsUrl);
                if (cookieHeader.length() > 0)
                    wsRequest.header("Cookie", cookieHeader.toString());

                WebSocket ws = client.newWebSocket(wsRequest.build(), new WebSocketListener() {
                    @Override public void onMessage(WebSocket webSocket, String text) {
                        if (epoch != generation) return;
                        readMessage(text);
                    }

                    @Override public void onMessage(WebSocket webSocket, ByteString bytes) {
                        if (epoch != generation) return;
                        readFrame(webSocket, bytes.toByteArray());
                    }

                    @Override public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                        if (epoch != generation) return;
                        connected = false;
                        publish(false, "连接断开：" + error.getMessage());
                    }

                    @Override public void onClosed(WebSocket webSocket, int code, String reason) {
                        if (epoch != generation) return;
                        connected = false;
                        publish(false, "电脑已断开连接");
                    }
                });
                if (epoch != generation) ws.cancel();
                else socket = ws;
            } catch (Exception ex) {
                if (epoch == generation) publish(false, "连接失败：" + ex.getMessage());
            }
        });
    }

    private void readMessage(String text) {
        try {
            JSONObject message = new JSONObject(text);
            switch (message.optString("t")) {
                case "hello": {
                    JSONObject g = message.getJSONObject("geom");
                    int width = g.getInt("width"), height = g.getInt("height");
                    int sw = g.getInt("sw"), sh = g.getInt("sh");
                    if (sw <= 0 || sh <= 0 || sw > 8192 || sh > 8192) return;
                    Geometry next = new Geometry(width, height, sw, sh);
                    synchronized (frameLock) {
                        if (frame == null || frame.getWidth() != sw || frame.getHeight() != sh) {
                            if (frame != null) frame.recycle();
                            frame = Bitmap.createBitmap(sw, sh, Bitmap.Config.ARGB_8888);
                        }
                        geometry = next;
                    }
                    mac = "mac".equals(message.optString("os", mac ? "mac" : "win"));
                    host = message.optString("host", host);
                    connected = true;
                    publish(true, "已连接 " + (host.isEmpty() ? "远程电脑" : host));
                    break;
                }
                case "locked":
                    publish(connected, "电脑已锁屏或处于安全桌面，无法显示画面");
                    break;
                case "toast":
                    // Nonfatal server notification.
                    publish(connected, message.optString("msg"));
                    break;
                default: break;
            }
        } catch (Exception ignored) {
            // Unexpected JSON must not close the session.
        }
    }

    private void readFrame(WebSocket ws, byte[] data) {
        long frameId = -1;
        try {
            ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
            if (b.remaining() < 7 || (b.get() & 0xff) != 1) return;
            frameId = b.getInt() & 0xffffffffL;
            int count = b.getShort() & 0xffff;
            if (count > 1024) return;
            synchronized (frameLock) {
                if (frame == null) return;
                Canvas canvas = new Canvas(frame);
                for (int i = 0; i < count; i++) {
                    if (b.remaining() < 12) return;
                    int x = b.getShort() & 0xffff;
                    int y = b.getShort() & 0xffff;
                    int w = b.getShort() & 0xffff;
                    int h = b.getShort() & 0xffff;
                    long len = b.getInt() & 0xffffffffL;
                    if (len <= 0 || len > 8_000_000 || len > b.remaining()) return;
                    int length = (int) len;
                    int pos = b.position();
                    if (w > 0 && h > 0 && x + w <= frame.getWidth()
                            && y + h <= frame.getHeight()) {
                        Bitmap patch = BitmapFactory.decodeByteArray(data, pos, length);
                        if (patch != null) {
                            canvas.drawBitmap(patch, null, new Rect(x, y, x + w, y + h), null);
                            patch.recycle();
                        }
                    }
                    b.position(pos + length);
                }
            }
            ui.post(listener::onFrame);
        } catch (Exception ignored) {
            // An invalid frame should be skipped, not kill the socket.
        } finally {
            if (frameId >= 0) {
                JSONObject ack = new JSONObject();
                try { ack.put("t", "ack"); ack.put("id", frameId); } catch (Exception ignored) {}
                ws.send(ack.toString());
            }
        }
    }

    private void publish(boolean active, String message) {
        ui.post(() -> listener.onConnection(active, message));
    }

    void send(JSONObject json) {
        WebSocket ws = socket;
        if (ws != null) ws.send(json.toString());
    }

    void send(String type, String code, boolean down) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("t", type);
            obj.put("code", code);
            obj.put("d", down);
            send(obj);
        } catch (Exception ignored) {}
    }

    void sendText(String value) {
        try {
            if (value == null || value.isEmpty()) return;
            for (int i = 0; i < value.length(); i += 4000) {
                JSONObject obj = new JSONObject();
                obj.put("t", "text");
                obj.put("s", value.substring(i, Math.min(value.length(), i + 4000)));
                send(obj);
            }
        } catch (Exception ignored) {}
    }

    void sendCombo(List<String> codes) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("t", "combo");
            obj.put("codes", new JSONArray(codes));
            send(obj);
        } catch (Exception ignored) {}
    }

    void mouse(String type, int x, int y, int button, int count, boolean down) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("t", type);
            obj.put("x", x); obj.put("y", y);
            if ("click".equals(type)) { obj.put("b", button); obj.put("n", count); }
            if ("mb".equals(type)) { obj.put("b", button); obj.put("d", down); }
            send(obj);
        } catch (Exception ignored) {}
    }

    void scroll(int x, int y) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("t", "wh");
            obj.put("dx", x); obj.put("dy", y);
            send(obj);
        } catch (Exception ignored) {}
    }

    void release() {
        try { JSONObject obj = new JSONObject(); obj.put("t", "release"); send(obj); }
        catch (Exception ignored) {}
    }

    void disconnect() {
        generation++;
        release();
        WebSocket old = socket;
        socket = null;
        if (old != null) old.close(1000, "client closed");
        connected = false;
        geometry = null;
        synchronized (frameLock) {
            if (frame != null) frame.recycle();
            frame = null;
        }
        publish(false, "已断开连接");
    }

    void shutdown() {
        disconnect();
        io.shutdownNow();
        client.dispatcher().executorService().shutdown();
        client.connectionPool().evictAll();
    }
}

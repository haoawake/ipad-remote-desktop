package dev.haoawake.remotedesktop;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.GestureDetector;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.view.View;
import android.view.ViewConfiguration;

/** Custom native drawing surface: remote screen, pointer, pinch zoom and touch control. */
final class RemoteSurface extends View {
    private final RemoteClient remote;
    private final Paint imagePaint = new Paint(Paint.FILTER_BITMAP_FLAG);
    private final Paint pointerPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final GestureDetector tapDetector;
    private final ScaleGestureDetector pinchDetector;
    private final RectF dest = new RectF();
    private final int slop;
    private float zoom = 1f;
    private float panX, panY;
    private float downX, downY, lastTwoX, lastTwoY;
    private long lastMove;
    private boolean dragging, moved, pinching, twoFingers;
    private boolean trackpad;
    private float pointerX, pointerY;
    private int mouseButton = 0;

    RemoteSurface(Context context, RemoteClient remote) {
        super(context);
        this.remote = remote;
        setFocusable(true);
        setFocusableInTouchMode(true);
        setBackgroundColor(Color.BLACK);
        slop = ViewConfiguration.get(context).getScaledTouchSlop();
        pointerPaint.setColor(Color.rgb(74, 166, 255));
        pointerPaint.setStyle(Paint.Style.STROKE);
        pointerPaint.setStrokeWidth(3f * getResources().getDisplayMetrics().density);
        tapDetector = new GestureDetector(context, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent event) { return true; }
            @Override public boolean onSingleTapConfirmed(MotionEvent e) {
                if (!remote.connected || moved || twoFingers) return true;
                int[] p = position(e.getX(), e.getY());
                if (p != null) {
                    remote.mouse("click", p[0], p[1], 0, 1, false);
                    pointerX = p[0]; pointerY = p[1];
                    invalidate();
                }
                return true;
            }
            @Override public boolean onDoubleTap(MotionEvent e) {
                if (!remote.connected) return true;
                int[] p = position(e.getX(), e.getY());
                if (p != null) remote.mouse("click", p[0], p[1], 0, 2, false);
                return true;
            }
            @Override public void onLongPress(MotionEvent e) {
                if (!remote.connected || dragging || moved || twoFingers) return;
                int[] p = position(e.getX(), e.getY());
                if (p != null) remote.mouse("click", p[0], p[1], 2, 1, false);
                moved = true;
            }
        });
        pinchDetector = new ScaleGestureDetector(context, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector detector) {
                zoom = Math.max(1f, Math.min(5f, zoom * detector.getScaleFactor()));
                pinching = true;
                moved = true;
                invalidate();
                return true;
            }
        });
    }

    void setTrackpad(boolean enabled) {
        trackpad = enabled;
    }

    void resetZoom() { zoom = 1f; panX = 0; panY = 0; invalidate(); }

    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        RemoteClient.Geometry g = remote.geometry;
        if (g == null) {
            Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
            p.setColor(Color.LTGRAY);
            p.setTextSize(18 * getResources().getDisplayMetrics().scaledDensity);
            canvas.drawText("等待远程画面…", 26, getHeight() / 2f, p);
            return;
        }
        updateRect(g);
        synchronized (remote.frameLock) {
            if (remote.frame != null && !remote.frame.isRecycled()) {
                canvas.drawBitmap(remote.frame, null, dest, imagePaint);
            }
        }
        if (trackpad) {
            float px = dest.left + pointerX / Math.max(1, g.width) * dest.width();
            float py = dest.top + pointerY / Math.max(1, g.height) * dest.height();
            canvas.drawCircle(px, py, 9 * getResources().getDisplayMetrics().density, pointerPaint);
        }
    }

    private void updateRect(RemoteClient.Geometry g) {
        float base = Math.min(getWidth() / (float) g.sw, getHeight() / (float) g.sh);
        float scale = base * zoom;
        float w = g.sw * scale, h = g.sh * scale;
        float x = (getWidth() - w) / 2f + panX;
        float y = (getHeight() - h) / 2f + panY;
        if (w > getWidth()) x = Math.max(getWidth() - w, Math.min(0, x));
        else x = (getWidth() - w) / 2f;
        if (h > getHeight()) y = Math.max(getHeight() - h, Math.min(0, y));
        else y = (getHeight() - h) / 2f;
        panX = x - (getWidth() - w) / 2f;
        panY = y - (getHeight() - h) / 2f;
        dest.set(x, y, x + w, y + h);
    }

    private int[] position(float x, float y) {
        RemoteClient.Geometry g = remote.geometry;
        if (g == null || dest.width() == 0 || dest.height() == 0) return null;
        float u = Math.max(0, Math.min(1, (x - dest.left) / dest.width()));
        float v = Math.max(0, Math.min(1, (y - dest.top) / dest.height()));
        return new int[] {Math.round(u * Math.max(0, g.width - 1)),
                Math.round(v * Math.max(0, g.height - 1))};
    }

    @Override public boolean onTouchEvent(MotionEvent e) {
        if (!remote.connected) return true;
        requestFocus();
        if (e.getToolType(0) == MotionEvent.TOOL_TYPE_MOUSE) return mouseEvent(e);

        pinchDetector.onTouchEvent(e);
        final int action = e.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) {
            twoFingers = false;
            pinching = false;
            moved = false;
            dragging = false;
            downX = e.getX(); downY = e.getY();
            lastMove = e.getEventTime();
            tapDetector.onTouchEvent(e);
            return true;
        }
        if (action == MotionEvent.ACTION_POINTER_DOWN && e.getPointerCount() >= 2) {
            if (dragging) {
                int[] p = position(e.getX(), e.getY());
                if (p != null) remote.mouse("mb", p[0], p[1], 0, 1, false);
                dragging = false;
            }
            twoFingers = true;
            moved = true;
            lastTwoX = (e.getX(0) + e.getX(1)) / 2f;
            lastTwoY = (e.getY(0) + e.getY(1)) / 2f;
            return true;
        }
        if (action == MotionEvent.ACTION_MOVE && e.getPointerCount() >= 2) {
            float mx = (e.getX(0) + e.getX(1)) / 2f;
            float my = (e.getY(0) + e.getY(1)) / 2f;
            float dx = mx - lastTwoX, dy = my - lastTwoY;
            if (!pinching) remote.scroll(Math.round(-dx * 2), Math.round(dy * 2));
            lastTwoX = mx; lastTwoY = my;
            invalidate();
            return true;
        }
        if (action == MotionEvent.ACTION_MOVE) {
            if (twoFingers) return true;
            tapDetector.onTouchEvent(e);
            float dx = e.getX() - downX, dy = e.getY() - downY;
            if (!moved && Math.hypot(dx, dy) > slop * 1.5) {
                moved = true;
                if (!trackpad) {
                    int[] start = position(downX, downY);
                    if (start != null) {
                        remote.mouse("mb", start[0], start[1], 0, 1, true);
                        dragging = true;
                    }
                }
            }
            if (moved) {
                if (trackpad) {
                    RemoteClient.Geometry g = remote.geometry;
                    if (g != null) {
                        float factor = g.width / (float) Math.max(1, dest.width());
                        pointerX = Math.max(0, Math.min(g.width - 1, pointerX + dx * factor));
                        pointerY = Math.max(0, Math.min(g.height - 1, pointerY + dy * factor));
                        remote.mouse("mm", Math.round(pointerX), Math.round(pointerY), 0, 1, false);
                        downX = e.getX(); downY = e.getY();
                    }
                } else {
                    int[] p = position(e.getX(), e.getY());
                    if (p != null) remote.mouse("mm", p[0], p[1], 0, 1, false);
                }
                invalidate();
            }
            return true;
        }
        if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            if (dragging) {
                int[] p = position(e.getX(), e.getY());
                if (p != null) remote.mouse("mb", p[0], p[1], 0, 1, false);
                dragging = false;
            } else if (trackpad && !twoFingers && !moved && action == MotionEvent.ACTION_UP) {
                remote.mouse("click", Math.round(pointerX), Math.round(pointerY), 0, 1, false);
                moved = true;
            }
            if (!twoFingers && !trackpad && action == MotionEvent.ACTION_UP) tapDetector.onTouchEvent(e);
            if (action == MotionEvent.ACTION_CANCEL) remote.release();
            invalidate();
            return true;
        }
        if (action == MotionEvent.ACTION_POINTER_UP && e.getPointerCount() == 2 && !pinching) {
            // A brief two-finger tap is the remote right click.
            if (e.getEventTime() - e.getDownTime() < 280) {
                int[] p = position(lastTwoX, lastTwoY);
                if (p != null) remote.mouse("click", p[0], p[1], 2, 1, false);
            }
            return true;
        }
        return true;
    }

    private boolean mouseEvent(MotionEvent e) {
        int[] p = position(e.getX(), e.getY());
        if (p == null) return true;
        int action = e.getActionMasked();
        int button = (e.getButtonState() & MotionEvent.BUTTON_SECONDARY) != 0 ? 2
                : (e.getButtonState() & MotionEvent.BUTTON_TERTIARY) != 0 ? 1 : 0;
        if (action == MotionEvent.ACTION_DOWN) {
            mouseButton = button;
            remote.mouse("mb", p[0], p[1], button, 1, true);
        } else if (action == MotionEvent.ACTION_MOVE) {
            remote.mouse("mm", p[0], p[1], 0, 1, false);
        } else if (action == MotionEvent.ACTION_UP) {
            remote.mouse("mb", p[0], p[1], mouseButton, 1, false);
        }
        return true;
    }

    @Override public boolean onGenericMotionEvent(MotionEvent event) {
        if (!remote.connected) return super.onGenericMotionEvent(event);
        if ((event.getSource() & InputDevice.SOURCE_CLASS_POINTER) == 0)
            return super.onGenericMotionEvent(event);
        int action = event.getActionMasked();
        if (action == MotionEvent.ACTION_SCROLL) {
            remote.scroll(Math.round(event.getAxisValue(MotionEvent.AXIS_HSCROLL) * 120),
                    Math.round(event.getAxisValue(MotionEvent.AXIS_VSCROLL) * 120));
            return true;
        }
        if (action == MotionEvent.ACTION_HOVER_MOVE) {
            int[] p = position(event.getX(), event.getY());
            if (p != null) remote.mouse("mm", p[0], p[1], 0, 1, false);
            return true;
        }
        return super.onGenericMotionEvent(event);
    }
}

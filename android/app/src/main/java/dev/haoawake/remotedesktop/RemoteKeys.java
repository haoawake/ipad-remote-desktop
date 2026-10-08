package dev.haoawake.remotedesktop;

import android.view.KeyEvent;
import java.util.ArrayList;
import java.util.List;

/** Android KeyEvent to the existing desktop service's KeyboardEvent.code mapping. */
final class RemoteKeys {
    private RemoteKeys() {}

    static String code(int key, boolean mac) {
        if (key >= KeyEvent.KEYCODE_A && key <= KeyEvent.KEYCODE_Z)
            return "Key" + (char) ('A' + key - KeyEvent.KEYCODE_A);
        if (key >= KeyEvent.KEYCODE_0 && key <= KeyEvent.KEYCODE_9)
            return "Digit" + (key - KeyEvent.KEYCODE_0);
        if (key >= KeyEvent.KEYCODE_NUMPAD_0 && key <= KeyEvent.KEYCODE_NUMPAD_9)
            return "Numpad" + (key - KeyEvent.KEYCODE_NUMPAD_0);
        if (key >= KeyEvent.KEYCODE_F1 && key <= KeyEvent.KEYCODE_F12)
            return "F" + (key - KeyEvent.KEYCODE_F1 + 1);
        switch (key) {
            case KeyEvent.KEYCODE_CTRL_LEFT: return "ControlLeft";
            case KeyEvent.KEYCODE_CTRL_RIGHT: return "ControlRight";
            case KeyEvent.KEYCODE_SHIFT_LEFT: return "ShiftLeft";
            case KeyEvent.KEYCODE_SHIFT_RIGHT: return "ShiftRight";
            case KeyEvent.KEYCODE_ALT_LEFT: return "AltLeft";
            case KeyEvent.KEYCODE_ALT_RIGHT: return "AltRight";
            case KeyEvent.KEYCODE_META_LEFT: return mac ? "MetaLeft" : "ControlLeft";
            case KeyEvent.KEYCODE_META_RIGHT: return mac ? "MetaRight" : "ControlRight";
            case KeyEvent.KEYCODE_ENTER: return "Enter";
            case KeyEvent.KEYCODE_NUMPAD_ENTER: return "NumpadEnter";
            case KeyEvent.KEYCODE_TAB: return "Tab";
            case KeyEvent.KEYCODE_ESCAPE: return "Escape";
            case KeyEvent.KEYCODE_DEL: return "Backspace";
            case KeyEvent.KEYCODE_FORWARD_DEL: return "Delete";
            case KeyEvent.KEYCODE_SPACE: return "Space";
            case KeyEvent.KEYCODE_DPAD_UP: return "ArrowUp";
            case KeyEvent.KEYCODE_DPAD_DOWN: return "ArrowDown";
            case KeyEvent.KEYCODE_DPAD_LEFT: return "ArrowLeft";
            case KeyEvent.KEYCODE_DPAD_RIGHT: return "ArrowRight";
            case KeyEvent.KEYCODE_MOVE_HOME: return "Home";
            case KeyEvent.KEYCODE_MOVE_END: return "End";
            case KeyEvent.KEYCODE_PAGE_UP: return "PageUp";
            case KeyEvent.KEYCODE_PAGE_DOWN: return "PageDown";
            case KeyEvent.KEYCODE_INSERT: return "Insert";
            case KeyEvent.KEYCODE_CAPS_LOCK: return "CapsLock";
            case KeyEvent.KEYCODE_MINUS: return "Minus";
            case KeyEvent.KEYCODE_EQUALS: return "Equal";
            case KeyEvent.KEYCODE_LEFT_BRACKET: return "BracketLeft";
            case KeyEvent.KEYCODE_RIGHT_BRACKET: return "BracketRight";
            case KeyEvent.KEYCODE_BACKSLASH: return "Backslash";
            case KeyEvent.KEYCODE_SEMICOLON: return "Semicolon";
            case KeyEvent.KEYCODE_APOSTROPHE: return "Quote";
            case KeyEvent.KEYCODE_GRAVE: return "Backquote";
            case KeyEvent.KEYCODE_COMMA: return "Comma";
            case KeyEvent.KEYCODE_PERIOD: return "Period";
            case KeyEvent.KEYCODE_SLASH: return "Slash";
            case KeyEvent.KEYCODE_NUMPAD_ADD: return "NumpadAdd";
            case KeyEvent.KEYCODE_NUMPAD_SUBTRACT: return "NumpadSubtract";
            case KeyEvent.KEYCODE_NUMPAD_MULTIPLY: return "NumpadMultiply";
            case KeyEvent.KEYCODE_NUMPAD_DIVIDE: return "NumpadDivide";
            case KeyEvent.KEYCODE_NUMPAD_DOT: return "NumpadDecimal";
            default: return null;
        }
    }

    static List<String> codes(String... codes) {
        ArrayList<String> out = new ArrayList<>();
        java.util.Collections.addAll(out, codes);
        return out;
    }

    static boolean isModifier(int key) {
        return key == KeyEvent.KEYCODE_CTRL_LEFT || key == KeyEvent.KEYCODE_CTRL_RIGHT
                || key == KeyEvent.KEYCODE_SHIFT_LEFT || key == KeyEvent.KEYCODE_SHIFT_RIGHT
                || key == KeyEvent.KEYCODE_ALT_LEFT || key == KeyEvent.KEYCODE_ALT_RIGHT
                || key == KeyEvent.KEYCODE_META_LEFT || key == KeyEvent.KEYCODE_META_RIGHT;
    }
}

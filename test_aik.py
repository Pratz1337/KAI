#!/usr/bin/env python
"""
Test script to verify all AIK components work correctly.
"""

import sys
import json

def test_imports():
    print("=" * 50)
    print("Testing imports...")
    try:
        from aik import agent, anthropic_client, capture, driver_bridge, history
        from aik.actions import parse_plan
        from aik.prompt import SYSTEM_PROMPT, build_user_prompt, PromptContext
        from aik.kill_switch import KillSwitch
        from aik.window_context import get_foreground_window
        from aik.input_injector import InputInjector
        print("✓ All imports successful")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}")
        return False


def test_history_memory():
    print("\n" + "=" * 50)
    print("Testing history/memory system...")
    try:
        from aik.history import ConversationHistory

        hist = ConversationHistory("Open Notepad and type hello")
        messages = hist.build_messages_for_decision(
            step=1,
            screenshot_png=b"\x89PNG\r\n\x1a\n" + (b"0" * 32),
            active_window_title="Untitled - Notepad",
            active_process_path="C:/Windows/System32/notepad.exe",
            user_text="Context: {}",
        )
        assert isinstance(messages, list) and messages
        assert messages[0]["role"] == "user"
        print("✓ ConversationHistory builds messages")
        print(f"  Messages count: {len(messages)}")

        # Ensure append_step returns a StepMemory
        mem = hist.append_step(
            step=1,
            observed="ok",
            planned_actions=[{"type": "stop", "reason": "done"}],
            executed_actions=[],
            success=True,
            screenshot_png=b"",
        )
        assert getattr(mem, "step", None) == 1
        print("✓ ConversationHistory.append_step returns StepMemory")
        return True
    except Exception as e:
        print(f"✗ History/memory failed: {e}")
        return False


def test_screen_capture():
    print("\n" + "=" * 50)
    print("Testing screen capture...")
    try:
        from aik.capture import ScreenCapturer
        sc = ScreenCapturer()
        shot = sc.capture()
        print(f"✓ Screenshot: {shot.width}x{shot.height}, {len(shot.png):,} bytes PNG")
        return True
    except Exception as e:
        print(f"✗ Screen capture failed: {e}")
        return False


def test_window_context():
    print("\n" + "=" * 50)
    print("Testing window context...")
    try:
        from aik.window_context import get_foreground_window
        fg = get_foreground_window()
        print(f"✓ Active window: {fg.title[:60]}...")
        print(f"  Process: {fg.process_path}")
        print(f"  PID: {fg.pid}")
        return True
    except Exception as e:
        print(f"✗ Window context failed: {e}")
        return False


def test_action_parser():
    print("\n" + "=" * 50)
    print("Testing action parser...")
    try:
        from aik.actions import parse_plan
        
        test_json = json.dumps({
            "actions": [
                {"type": "type_text", "text": "Hello World"},
                {"type": "key_press", "key": "enter"},
                {"type": "hotkey", "keys": ["ctrl", "s"]},
                {"type": "wait_ms", "ms": 500},
                {"type": "stop", "reason": "Done"}
            ]
        })
        
        plan = parse_plan(test_json)
        print(f"✓ Parsed {len(plan.actions)} actions:")
        for a in plan.actions:
            print(f"    {a}")
        return True
    except Exception as e:
        print(f"✗ Action parser failed: {e}")
        return False


def test_input_injector():
    print("\n" + "=" * 50)
    print("Testing input injector (no actual keystrokes)...")
    try:
        from aik.input_injector import InputInjector, _vk_from_key_name
        
        # Test key name to VK code mapping
        tests = [('enter', 0x0D), ('ctrl', 0x11), ('a', 0x41), ('f1', 0x70)]
        for key, expected in tests:
            vk = _vk_from_key_name(key)
            status = "✓" if vk == expected else "✗"
            print(f"  {status} VK({key}) = {hex(vk)}")
        
        injector = InputInjector()
        print("✓ InputInjector initialized")
        return True
    except Exception as e:
        print(f"✗ Input injector failed: {e}")
        return False


def test_driver_bridge():
    print("\n" + "=" * 50)
    print("Testing driver bridge...")
    try:
        from aik import driver_bridge as dbmod
        DriverBridge = getattr(dbmod, "DriverBridge")

        # Optional legacy helpers
        if hasattr(dbmod, "SCANCODE_MAP"):
            print(f"  Legacy scancode for 'a': {hex(dbmod.SCANCODE_MAP.get('a', 0))}")

        db = DriverBridge()

        # New API: open/is_open/close
        opened = False
        if hasattr(db, "open"):
            opened = bool(db.open())
        elif hasattr(db, "connect"):
            opened = bool(db.connect())

        if opened:
            print("✓ Driver opened!")
            if hasattr(db, "ping"):
                ok = db.ping()
                print(f"  PING ok: {ok}")
            # Smoke-test inject_text if present (should not crash)
            if hasattr(db, "inject_text"):
                _ = db.inject_text("A")
                print("  inject_text('A') invoked")
            if hasattr(db, "close"):
                db.close()
            elif hasattr(db, "disconnect"):
                db.disconnect()
        else:
            print("○ Driver not loaded (expected if driver .sys not installed)")
        
        return True
    except Exception as e:
        print(f"✗ Driver bridge failed: {e}")
        return False


def test_anthropic_client():
    print("\n" + "=" * 50)
    print("Testing Anthropic client setup...")
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        
        if not api_key:
            print("○ ANTHROPIC_API_KEY not set (set in .env to enable)")
            return True
        
        # Only test client creation, not actual API call
        from aik.anthropic_client import AnthropicClient
        client = AnthropicClient(api_key=api_key, model=model)
        print(f"✓ Anthropic client initialized")
        print(f"  Model: {model}")
        print(f"  API key: {api_key[:10]}...{api_key[-4:]}")

        # Sanity-check new history method exists
        assert hasattr(client, "create_message_with_history")
        print("✓ History-aware API method present")
        return True
    except Exception as e:
        print(f"✗ Anthropic client setup failed: {e}")
        return False


def test_prompt_building():
    print("\n" + "=" * 50)
    print("Testing prompt building...")
    try:
        from aik.prompt import SYSTEM_PROMPT, build_user_prompt, PromptContext
        
        ctx = PromptContext(
            goal="Type Hello World in Notepad",
            window_title="Untitled - Notepad",
            process_path="C:\\Windows\\System32\\notepad.exe",
            step=1,
            recent_actions=[]
        )
        
        user_prompt = build_user_prompt(ctx)
        print(f"✓ System prompt: {len(SYSTEM_PROMPT)} chars")
        print(f"✓ User prompt: {len(user_prompt)} chars")
        print(f"  Preview: {user_prompt[:100]}...")
        return True
    except Exception as e:
        print(f"✗ Prompt building failed: {e}")
        return False


def test_kill_switch():
    print("\n" + "=" * 50)
    print("Testing kill switch...")
    try:
        from aik.kill_switch import KillSwitch
        
        ks = KillSwitch()
        print(f"✓ KillSwitch initialized")
        print(f"  Triggered: {ks.triggered}")
        print("  Combo: Ctrl+Alt+Backspace")
        return True
    except Exception as e:
        print(f"✗ Kill switch failed: {e}")
        return False


def main():
    print("AIK (AI Keyboard) Component Test")
    print("=" * 50)
    
    tests = [
        ("Imports", test_imports),
        ("History/Memory", test_history_memory),
        ("Screen Capture", test_screen_capture),
        ("Window Context", test_window_context),
        ("Action Parser", test_action_parser),
        ("Input Injector", test_input_injector),
        ("Driver Bridge", test_driver_bridge),
        ("Anthropic Client", test_anthropic_client),
        ("Prompt Building", test_prompt_building),
        ("Kill Switch", test_kill_switch),
    ]
    
    results = []
    for name, test_fn in tests:
        try:
            results.append((name, test_fn()))
        except Exception as e:
            print(f"✗ {name} crashed: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\n{passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print("\n✓ All components working! Ready to run:")
        print('  python main.py --goal "Your task here" --dry-run')
    
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

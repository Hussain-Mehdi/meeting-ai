import subprocess


def notify(title: str, message: str, sound=False):
    safe_title = title.replace('"', '\\"'); safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"' + (' sound name "default"' if sound else '')
    try: subprocess.run(["osascript", "-e", script], timeout=5, check=False)
    except Exception: pass


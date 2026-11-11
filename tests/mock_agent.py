import sys
import time

def main():
    print("MOCK_AGENT_STARTED", flush=True)
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line_str = line.strip()
            if line_str:
                print(f"MOCK_AGENT_ECHO: {line_str}", flush=True)
                if "EXIT" in line_str:
                    break
        except (KeyboardInterrupt, EOFError):
            break

if __name__ == "__main__":
    main()

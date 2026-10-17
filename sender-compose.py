"""Cross-platform sender launcher. Run: python3 sender-compose.py up -d."""
from backupkit.compose import main

if __name__ == "__main__":
    raise SystemExit(main())

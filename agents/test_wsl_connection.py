"""
Test WSL connection and Ollama availability.

Run this before using the orchestrator to verify your setup.
"""

import subprocess
import sys


def test_wsl():
    """Test if WSL is available."""
    print("Testing WSL availability...")
    try:
        result = subprocess.run(
            ["wsl", "echo", "WSL is working"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            print(f"✓ WSL is available: {result.stdout.strip()}")
            return True
        else:
            print(f"✗ WSL failed: {result.stderr}")
            return False

    except FileNotFoundError:
        print("✗ WSL not found. Install WSL first.")
        return False
    except Exception as e:
        print(f"✗ WSL test failed: {e}")
        return False


def test_ollama():
    """Test if Ollama is available in WSL."""
    print("\nTesting Ollama in WSL...")
    try:
        result = subprocess.run(
            ["wsl", "bash", "-c", "which ollama"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            print(f"✓ Ollama found at: {result.stdout.strip()}")
            return True
        else:
            print("✗ Ollama not found in WSL")
            print("  Install: curl https://ollama.ai/install.sh | sh")
            return False

    except Exception as e:
        print(f"✗ Ollama test failed: {e}")
        return False


def test_model(model="qwen2.5-coder:7b"):
    """Test if specific model is available."""
    print(f"\nTesting model '{model}'...")
    try:
        result = subprocess.run(
            ["wsl", "bash", "-c", "ollama list"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            if model in result.stdout:
                print(f"✓ Model '{model}' is available")
                return True
            else:
                print(f"✗ Model '{model}' not found")
                print(f"  Pull it: wsl bash -c 'ollama pull {model}'")
                print("\nAvailable models:")
                print(result.stdout)
                return False
        else:
            print(f"✗ Failed to list models: {result.stderr}")
            return False

    except Exception as e:
        print(f"✗ Model test failed: {e}")
        return False


def test_inference(model="qwen2.5-coder:7b"):
    """Test actual model inference."""
    print(f"\nTesting inference with '{model}'...")
    try:
        prompt = "Respond with exactly: OK"
        cmd = [
            "wsl",
            "bash",
            "-c",
            f"ollama run {model} '{prompt}'"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            response = result.stdout.strip()
            print(f"✓ Model responded: {response[:100]}")
            return True
        else:
            print(f"✗ Inference failed: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        print("✗ Inference timed out (model may be loading)")
        return False
    except Exception as e:
        print(f"✗ Inference test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("WSL + Ollama Setup Test")
    print("=" * 60)

    tests = [
        ("WSL", test_wsl),
        ("Ollama", test_ollama),
        ("Model", test_model),
        ("Inference", test_inference),
    ]

    results = {}
    for name, test_func in tests:
        results[name] = test_func()
        if not results[name]:
            print(f"\n✗ Test '{name}' failed. Fix this before continuing.\n")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("✓ All tests passed! Ready to run orchestrator.")
    print("=" * 60)


if __name__ == "__main__":
    main()

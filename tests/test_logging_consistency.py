import ast
from pathlib import Path
import pytest

# Path to the server's main file
SERVER_MAIN_PATH = Path(__file__).parent.parent / "server" / "main.py"

class DirectOutputVisitor(ast.NodeVisitor):
    def __init__(self):
        self.violations = []
        self._in_main_block = False # To exempt startup prints if necessary

    def visit_If(self, node: ast.If):
        # Check for if __name__ == "__main__": block
        if (isinstance(node.test, ast.Compare) and
            isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__' and
            isinstance(node.test.ops[0], ast.Eq) and
            isinstance(node.test.comparators[0], ast.Constant) and node.test.comparators[0].value == '__main__'):
            self._in_main_block = True
            self.generic_visit(node) # Visit children of the if block
            self._in_main_block = False
        else:
            self.generic_visit(node) # Continue visiting normally

    def visit_Call(self, node: ast.Call):
        # Check for print() calls
        if isinstance(node.func, ast.Name) and node.func.id == 'print':
            # Allow prints in the main block for startup messages, assuming they are then replaced by logger.info
            # Since we just replaced them, this exemption is technically not needed anymore,
            # but it demonstrates how to define exempt contexts.
            # However, the strict rule is "no other way", so even in main block, it should be logger.
            self.violations.append(f"Direct 'print()' call found at line {node.lineno}")
        
        # Check for sys.stdout.write() or sys.stderr.write() calls
        elif (isinstance(node.func, ast.Attribute) and
              isinstance(node.func.value, ast.Attribute) and
              isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == 'sys' and
              node.func.attr == 'write' and
              (node.func.value.attr == 'stdout' or node.func.value.attr == 'stderr')):
            self.violations.append(f"Direct 'sys.{node.func.value.attr}.write()' call found at line {node.lineno}")

        self.generic_visit(node) # Continue visiting child nodes


def test_no_direct_console_output_in_server_main():
    """
    Ensures that server/main.py does not use direct print() or sys.stdout/stderr.write()
    calls, enforcing consistent logging via the logger instance.
    """
    with open(SERVER_MAIN_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=SERVER_MAIN_PATH)

    visitor = DirectOutputVisitor()
    visitor.visit(tree)

    if visitor.violations:
        error_message = (
            "Found direct console output calls in server/main.py. "
            "All output must use the configured logging system.\n"
            f"{chr(10).join(visitor.violations)}" # Use chr(10) for newline if direct \n is problematic
        )
        pytest.fail(error_message)

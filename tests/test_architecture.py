import ast
from pathlib import Path


def _get_src_dir() -> Path:
    """
    Return the project's src directory robustly by walking up the directory
    tree from this file until a 'src' directory is found.
    """
    current_path = Path(__file__).resolve()
    for parent in current_path.parents:
        candidate = parent / "src"
        if candidate.is_dir():
            return candidate
            
    raise FileNotFoundError(f"Could not locate 'src' directory starting from {current_path}")


def _is_forbidden_module(module_name: str, forbidden_name: str = "experiments") -> bool:
    """
    Check if the imported module exactly matches forbidden_name or is a submodule.
    
    Examples for forbidden_name='experiments':
    - 'experiments' -> True
    - 'experiments.models' -> True
    - 'experiments_utils' -> False (Prevents false positives)
    """
    if not module_name:
        return False
    return module_name == forbidden_name or module_name.startswith(f"{forbidden_name}.")


def _check_file_for_forbidden_imports(
    source_path: Path, forbidden_name: str = "experiments"
) -> tuple[bool, bool]:
    """
    Parses the Python file and checks for forbidden imports.
    Returns a tuple: (has_forbidden_import, has_syntax_error)
    """
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (SyntaxError, UnicodeDecodeError):
        # Do not silently skip unparseable files. Flag them for failure.
        return False, True

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # Handles: import experiments, import experiments.models
            for alias in node.names:
                if _is_forbidden_module(alias.name, forbidden_name):
                    return True, False
                    
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and _is_forbidden_module(node.module, forbidden_name)
        ):
            # Handles: from experiments import xyz, from experiments.models import xyz
            return True, False

    return False, False


def test_production_code_does_not_import_experiments() -> None:
    """
    Ensure that no production code under src/ imports from the experiments package.

    This enforces a clean separation between production code and experimental
    / research code, so that production artifacts never depend on unstable
    experimental modules.
    """
    src_dir = _get_src_dir()
    assert src_dir.exists(), f"Source directory not found: {src_dir}"

    forbidden_files: list[Path] = []
    unparseable_files: list[Path] = []

    for path in src_dir.rglob("*.py"):
        has_forbidden, has_syntax_error = _check_file_for_forbidden_imports(path, "experiments")
        
        if has_forbidden:
            forbidden_files.append(path)
        if has_syntax_error:
            unparseable_files.append(path)

    # Accumulate all architectural and structural errors for a single, comprehensive report
    errors: list[str] = []
    
    if forbidden_files:
        formatted_files = "\n".join(f"  - {p.relative_to(src_dir.parent)}" for p in forbidden_files)
        errors.append(
            f"Production code must not import from 'experiments'.\n"
            f"Offending files ({len(forbidden_files)}):\n{formatted_files}"
        )
        
    if unparseable_files:
        formatted_files = "\n".join(f"  - {p.relative_to(src_dir.parent)}" for p in unparseable_files)
        errors.append(
            f"Could not parse some production files (SyntaxError or Encoding issues).\n"
            f"These files must be valid Python 3 UTF-8:\n{formatted_files}"
        )

    # Fail the test gracefully with all context if any violations or unparseable files exist
    assert not errors, "\n\n".join(errors)
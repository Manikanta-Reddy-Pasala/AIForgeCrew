"""An unresolved type in a build: say whether it exists and the closest real names."""
import subprocess
import zipfile

from aiforge_core.runtime import symbol_lookup as sl
from aiforge_core.runtime.chat_agent._obs_text import render_command, render_job

KT = ("e: file:///r/src/main/kotlin/Rule.kt:42:33 Unresolved reference 'KtPropertyAccessExpression'.\n"
      "> Task :rules:compileKotlin FAILED")
JAVAC = ("[ERROR] /r/src/main/java/a/Svc.java:[10,5] cannot find symbol\n"
         "  symbol:   class OrderRepo\n  location: class a.Svc")


def _setup(tmp_path, monkeypatch):
    cache = tmp_path / "m2"
    cache.mkdir()
    with zipfile.ZipFile(cache / "psi-1.0.jar", "w") as z:
        for c in ("KtDotQualifiedExpression", "KtQualifiedExpression", "KtNameReferenceExpression"):
            z.writestr(f"org/jetbrains/kotlin/psi/{c}.class", b"")
        z.writestr("org/jetbrains/kotlin/psi/KtQualifiedExpression$Inner.class", b"")
    monkeypatch.setattr(sl, "_JAR_ROOTS", (str(cache),))
    sl._JARS.update(at=0.0, names={})
    sl._SRC.clear()
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src/OrderRepository.java").write_text("public class OrderRepository {}\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return str(repo)


def test_invented_type_is_named_absent_with_closest_real_names(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    line = sl.note(KT, root)
    assert "`KtPropertyAccessExpression` does not exist" in line
    assert "KtQualifiedExpression" in line and "do not search" in line


def test_a_type_that_exists_says_where(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    line = sl.note(KT.replace("KtPropertyAccessExpression", "KtDotQualifiedExpression"), root)
    assert "org.jetbrains.kotlin.psi.KtDotQualifiedExpression" in line and "import it" in line
    line = sl.note(JAVAC.replace("OrderRepo", "OrderRepository"), root)
    assert "exists in this project" in line and "OrderRepository.java" in line


def test_javac_names_and_nothing_for_other_failures(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    assert sl.unresolved(JAVAC) == ["OrderRepo"]
    assert sl.note("Tests run: 5, Failures: 1", root) is None
    monkeypatch.setenv("AIFORGE_CHAT_SYMBOL_LOOKUP", "0")
    assert sl.note(KT, root) is None


def test_advice_reaches_the_model_text():
    r = {"code": 1, "stdout": "x" * 50000, "stderr": "",
         "symbols": "symbol check: `Foo` does not exist", "reused": "code unchanged since last run"}
    out = render_command(r, 3000)
    assert "[symbol check: `Foo` does not exist]" in out[:400] and "code unchanged" in out[:400]
    j = render_job({"id": "bg-1", "code": 1, "running": False, "new_output": "boom",
                    "toolchain": "JDK 24 used"}, 3000)
    assert "[JDK 24 used]" in j

"""JavaSim: JVM bootstrap, JAR building and the JPype bridge."""
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from .config import APP_DATA_DIR, logger

# Попытка импорта JPype для Java-интеграции
# FIX #8: jpype импортируется внутри try, иначе крэш при старте если jpype не установлен
try:
    import jpype
    import jpype.imports
    JAVA_OK = True
except ImportError:
    jpype = None
    JAVA_OK = False


JAVA_SOURCE_RELATIVE = os.path.join("java", "com", "hoi4", "translator", "HoI4Translator.java")
BUNDLED_JAR_NAMES = ("hoi4_translator.jar", "HoI4Translator.jar")


def build_java_jar(source_root: str, output_jar: str) -> Optional[str]:
    """Compile the bundled Java service into a JAR.

    Returns the JAR path, or None when no JDK is installed or compilation
    fails. The JAR is intentionally not committed: a binary can silently drift
    away from java/, and CI builds it for releases.
    """
    source_file = os.path.join(source_root, JAVA_SOURCE_RELATIVE)
    if not os.path.isfile(source_file):
        return None
    javac = shutil.which("javac")
    jar_tool = shutil.which("jar")
    if not javac or not jar_tool:
        logger.info("JDK не найден: JAR для JavaSim не будет собран автоматически")
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="hoi4-java-") as classes:
            compiled = subprocess.run(
                [javac, "-d", classes, source_file],
                capture_output=True, text=True, timeout=180,
            )
            if compiled.returncode != 0:
                logger.warning(
                    "Не удалось скомпилировать JavaSim: %s",
                    (compiled.stderr or "").strip()[:500],
                )
                return None
            os.makedirs(os.path.dirname(output_jar) or ".", exist_ok=True)
            packaged = subprocess.run(
                [jar_tool, "cf", output_jar, "-C", classes, "com"],
                capture_output=True, text=True, timeout=120,
            )
            if packaged.returncode != 0:
                logger.warning(
                    "Не удалось упаковать JAR JavaSim: %s",
                    (packaged.stderr or "").strip()[:500],
                )
                return None
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Сборка JAR JavaSim прервана: %s", error)
        return None

    logger.info("JAR JavaSim собран: %s", output_jar)
    return output_jar


def resolve_java_jar(configured: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    """Find the JavaSim JAR: explicit path, bundled copy, or build from source."""
    if configured and os.path.exists(configured):
        return configured

    if base_dir is None:
        # PyInstaller unpacks bundled files into a temporary directory.
        base_dir = (
            sys._MEIPASS if hasattr(sys, "_MEIPASS")
            else os.path.dirname(os.path.abspath(__file__))
        )
    for name in BUNDLED_JAR_NAMES:
        candidate = os.path.join(base_dir, name)
        if os.path.exists(candidate):
            return candidate

    built_jar = os.path.join(APP_DATA_DIR, "hoi4_translator.jar")
    source_file = os.path.join(base_dir, JAVA_SOURCE_RELATIVE)
    if os.path.exists(built_jar) and os.path.isfile(source_file):
        try:
            if os.path.getmtime(built_jar) >= os.path.getmtime(source_file):
                return built_jar
        except OSError:
            pass
    return build_java_jar(base_dir, built_jar)


class JavaTranslatorService:
    """Thin JPype wrapper around the bundled Java translation service."""

    # The JVM classpath is fixed once the JVM is running: keep the JAR that was
    # used so a later change of the JAR path can be reported honestly.
    _started_jar: Optional[str] = None
    _warned_about_outdated_jar = False

    def __init__(self, jar_path: Optional[str] = None):
        self.java_available = JAVA_OK and jar_path is not None and os.path.exists(jar_path)
        self.jar_path = jar_path
        self._jvm_started = False

        if self.java_available:
            self._start_jvm()
        else:
            if not JAVA_OK:
                logger.warning("JPype не установлен. pip install jpype1")
            elif not jar_path:
                logger.info("Путь к Java JAR не указан.")
            else:
                logger.error(f"Файл JAR не найден: {jar_path}")

    @staticmethod
    def _jvm_library_names() -> Tuple[str, ...]:
        if os.name == "nt":
            return ("jvm.dll",)
        if sys.platform == "darwin":
            return ("libjvm.dylib", "libjvm.so")
        return ("libjvm.so",)

    @classmethod
    def _jvm_candidates(cls, java_home: str) -> List[str]:
        """Build plausible JVM library paths for the current platform."""
        candidates = []
        for sub in ("bin", os.path.join("bin", "server"), os.path.join("bin", "client"),
                    os.path.join("jre", "bin", "server"), "lib", os.path.join("lib", "server")):
            for name in cls._jvm_library_names():
                candidates.append(os.path.join(java_home, sub, name))
        return candidates

    def _resolve_jvm_path(self) -> str:
        """Locate the JVM library, starting with JPype's own cross-platform lookup."""
        candidates: List[str] = []

        # 1. Explicit override (also honoured by JPype itself).
        override = os.environ.get("JPYPE_JVM_PATH")
        if override:
            candidates.append(override)

        # 2. JPype's own detection: correct on every supported platform, and it
        #    already returns a file path (a directory only on exotic setups).
        try:
            default_path = jpype.getDefaultJVMPath()
        except Exception as error:  # pragma: no cover - depends on the JVM install
            logger.debug("jpype.getDefaultJVMPath() failed: %s", error)
        else:
            if default_path:
                if os.path.isdir(default_path):
                    candidates.extend(
                        os.path.join(default_path, name)
                        for name in self._jvm_library_names()
                    )
                else:
                    candidates.append(default_path)

        # 3. Build candidates from JAVA_HOME as a last resort.
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidates.extend(self._jvm_candidates(java_home))

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate

        searched = "; ".join(candidate for candidate in candidates if candidate) or "не найдено"
        raise FileNotFoundError(f"Не найдена библиотека JVM ({searched})")

    def _start_jvm(self):
        """Безопасный запуск JVM с приоритетом JAVA_HOME"""
        if jpype.isJVMStarted():
            self._jvm_started = True
            self._check_jar_matches_running_jvm()
            return

        try:
            jvm_path = self._resolve_jvm_path()

            logger.info(f" Запуск JVM: {jvm_path}")
            jpype.startJVM(jvm_path, classpath=[self.jar_path], convertStrings=False)
            self._jvm_started = True
            JavaTranslatorService._started_jar = self.jar_path
            logger.info("✅ JVM успешно запущен.")
        except Exception as e:
            logger.error(f"❌ Ошибка запуска JVM: {e}")
            self.java_available = False

    def _check_jar_matches_running_jvm(self):
        """Warn when a different JAR was requested but cannot be loaded."""
        started_jar = JavaTranslatorService._started_jar
        if not started_jar or not self.jar_path:
            return
        if (
            os.path.normcase(os.path.abspath(started_jar))
            != os.path.normcase(os.path.abspath(self.jar_path))
            and not JavaTranslatorService._warned_about_outdated_jar
        ):
            JavaTranslatorService._warned_about_outdated_jar = True
            logger.warning(
                "JVM уже запущена с JAR %s; новый JAR %s будет использован только "
                "после перезапуска приложения",
                started_jar, self.jar_path,
            )

    def translate_batch_java(self, texts: List[str], target_lang: str = "ru",
                             source_lang: Optional[str] = None) -> List[str]:
        if not self.java_available or not self._jvm_started:
            raise RuntimeError("JavaSim недоступен: JVM или JAR не загружен")

        try:
            # Пакет и класс должны точно совпадать с тем, что в .java файле
            JTranslator = jpype.JClass("com.hoi4.translator.HoI4Translator")
            translator = JTranslator()
            return [self._translate_one(translator, t, target_lang, source_lang) for t in texts]
        except Exception as e:
            logger.exception("Java translation error")
            raise RuntimeError(f"Ошибка JavaSim: {e}") from e

    @staticmethod
    def _translate_one(translator, text: str, target_lang: str, source_lang: Optional[str]) -> str:
        """Pass the selected source language when the JAR supports it."""
        if source_lang:
            try:
                return str(translator.translate(text, target_lang, source_lang))
            except TypeError:
                # An older JAR only exposes translate(text, targetLang), which
                # asks Google to auto-detect the source language.
                if not JavaTranslatorService._warned_about_outdated_jar:
                    JavaTranslatorService._warned_about_outdated_jar = True
                    logger.warning(
                        "Используется устаревший HoI4Translator.jar: исходный язык "
                        "не передаётся (sl=auto). Пересоберите JAR из java/."
                    )
            except Exception:
                # Any other failure means the overload exists but did not work.
                raise
        return str(translator.translate(text, target_lang))

    def shutdown(self):
        """Release references; the JVM itself cannot be restarted in-process.

        JPype never unloads a started JVM, so this intentionally does not
        pretend to stop it. It only records the state for the logs.
        """
        if self._jvm_started and jpype.isJVMStarted():
            logger.info("JavaSim завершил работу (JVM остаётся запущенной до выхода).")
        self._jvm_started = False

"""
streamlit_stub.py -- minimaler Streamlit-Ersatz für Tests.

Warum: ``streamlit run app.py`` lässt sich in einer Testumgebung nicht
sinnvoll starten, aber genau dort passieren die Fehler, die man sonst erst
im Browser sieht -- ein Tippfehler in einem Variablennamen, ein Aufruf mit
falscher Signatur, ein None, das durchrutscht.

Dieser Stub stellt die benutzten Streamlit-Bausteine nach, gibt für jedes
Eingabeelement den Vorgabewert zurück und protokolliert alles. Damit lässt
sich app.py komplett durchlaufen lassen und prüfen, ob die Seite ohne
Ausnahme aufgebaut wird und die erwarteten Inhalte enthält.

Was der Stub NICHT prüft: Layout, Aussehen, Interaktion. Dafür braucht es
einen echten Streamlit-Start.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Any


class StopExecution(Exception):
    """Entspricht st.stop()."""


class SessionState(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v


class _Status:
    def __init__(self, recorder, label):
        self.recorder = recorder
        self.label = label
        self.state = "running"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, label=None, state=None, expanded=None):
        if label:
            self.label = label
        if state:
            self.state = state

    def write(self, *a, **k):
        self.recorder.calls.append(("status.write", a, k))


class StreamlitStub(types.ModuleType):
    """Ein Modul-Objekt, das sich wie ``streamlit`` benutzen lässt."""

    def __init__(self, name: str = "streamlit"):
        super().__init__(name)
        self.calls: list[tuple] = []
        self.texts: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.session_state = SessionState()
        self.stopped = False
        self.button_returns: dict[str, bool] = {}
        self.components = types.ModuleType("streamlit.components")
        v1 = types.ModuleType("streamlit.components.v1")
        v1.html = self._record_html
        self.components.v1 = v1
        self.html_payloads: list[str] = []

    # -- Protokoll ---------------------------------------------------------
    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))

    def _record_html(self, html, **k):
        self.html_payloads.append(html)
        self._rec("components.html", len(html), k)

    # -- Ausgabe -----------------------------------------------------------
    def _text(self, name):
        def fn(*a, **k):
            if a and isinstance(a[0], str):
                self.texts.append(a[0])
            self._rec(name, *a, **k)
        return fn

    def __getattr__(self, name):
        # Alles, was nicht ausdrücklich definiert ist, wird protokolliert und
        # gibt None zurück -- so bricht der Stub nicht an einer Randfunktion.
        if name.startswith("_"):
            raise AttributeError(name)
        return self._text(name)

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))
        self._rec("error", msg)

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))
        self._rec("warning", msg)

    def info(self, msg, *a, **k):
        self.texts.append(str(msg))
        self._rec("info", msg)

    def success(self, msg, *a, **k):
        self.texts.append(str(msg))
        self._rec("success", msg)

    def markdown(self, body="", **k):
        self.texts.append(str(body))
        self._rec("markdown", len(str(body)))

    def caption(self, body="", **k):
        self.texts.append(str(body))
        self._rec("caption", body)

    def title(self, body="", **k):
        self.texts.append(str(body))
        self._rec("title", body)

    def stop(self):
        self.stopped = True
        raise StopExecution()

    # -- Layout ------------------------------------------------------------
    def set_page_config(self, **k):
        self._rec("set_page_config", k)

    def tabs(self, labels):
        self._rec("tabs", labels)
        return [_Container(self, f"tab:{lbl}") for lbl in labels]

    def columns(self, spec, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Container(self, f"col{i}") for i in range(n)]

    @contextmanager
    def expander(self, label, expanded=False):
        self._rec("expander", label)
        yield _Container(self, f"expander:{label}")

    @contextmanager
    def spinner(self, text=""):
        yield None

    def status(self, label, expanded=False):
        self._rec("status", label)
        return _Status(self, label)

    def container(self, **k):
        return _Container(self, "container")

    # -- Eingaben: liefern den Vorgabewert ---------------------------------
    def slider(self, label, min_value=None, max_value=None, value=None, step=None,
               *a, **k):
        self._rec("slider", label, value)
        return value if value is not None else (min_value if min_value is not None else 0)

    def number_input(self, label, min_value=None, max_value=None, value=0.0,
                     step=None, *a, **k):
        self._rec("number_input", label, value)
        return value

    def checkbox(self, label, value=False, *a, **k):
        self._rec("checkbox", label, value)
        return value

    def toggle(self, label, value=False, **k):
        self._rec("toggle", label, value)
        return value

    def radio(self, label, options, index=0, key=None, *a, **k):
        self._rec("radio", label, options)
        choice = list(options)[index]
        if key:
            self.session_state.setdefault(key, choice)
            return self.session_state[key]
        return choice

    def selectbox(self, label, options, index=0, key=None, format_func=None,
                  *a, **k):
        opts = list(options)
        self._rec("selectbox", label, opts)
        if not opts:
            return None
        choice = opts[index]
        if key:
            self.session_state.setdefault(key, choice)
            return self.session_state[key]
        return choice

    def button(self, label, **k):
        self._rec("button", label)
        return bool(self.button_returns.get(label, False))

    def download_button(self, label, data=None, file_name=None, mime=None, **k):
        self._rec("download_button", label, file_name, len(data) if data is not None else 0)
        return False

    def metric(self, label, value, **k):
        self._rec("metric", label, value)

    def dataframe(self, df, **k):
        self._rec("dataframe", getattr(df, "shape", None))

    def json(self, obj, **k):
        self._rec("json", type(obj).__name__)

    def code(self, body, **k):
        self._rec("code", len(str(body)))

    def subheader(self, body, **k):
        self.texts.append(str(body))
        self._rec("subheader", body)

    # -- Caching: einfach durchreichen -------------------------------------
    def cache_data(self, func=None, **kwargs):
        def wrap(f):
            def inner(*a, **k):
                return f(*a, **k)
            inner.clear = lambda: None
            inner.__name__ = getattr(f, "__name__", "cached")
            return inner
        return wrap(func) if callable(func) else wrap

    cache_resource = cache_data


class _Container:
    """Steht für tab / column / expander -- kann alles, was der Stub kann."""

    def __init__(self, st: StreamlitStub, name: str):
        self._st = st
        self._name = name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, item):
        return getattr(self._st, item)


def install() -> StreamlitStub:
    """Registriert den Stub als ``streamlit`` in sys.modules."""
    stub = StreamlitStub()
    sys.modules["streamlit"] = stub
    sys.modules["streamlit.components"] = stub.components
    sys.modules["streamlit.components.v1"] = stub.components.v1
    return stub


def uninstall() -> None:
    for name in ("streamlit", "streamlit.components", "streamlit.components.v1"):
        sys.modules.pop(name, None)

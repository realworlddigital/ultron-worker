"""Ultron local job runner (tsk_da8f7875f31e4d8298f4).

Backbone cards of type ``task.local_job`` with ``data.executor == "local-ultron"`` run on
local models on David's Surface ("Ultron") instead of Devin ACU or API tokens. The runner
only ever calls out (HTTPS polling of the backbone, localhost Ollama); its status GUI binds
to localhost and is a view, never a control plane.
"""

__version__ = "0.2.1"

"""Delivery-layer-agnostic orchestration of the diagnosis pipeline.

``run_diagnosis_pipeline`` (Planner → seed → Diagnostician → assemble ``Report``)
used to live in ``cli/_intent.py``. It is hoisted here so both delivery layers —
the CLI (``cli/_intent.py``) and the Scheduler (``scheduler/runner.py``) — depend
on ``orchestration`` rather than the Scheduler reaching back into the CLI layer
(design D-2). The module pulls in no Rich / Typer / CLI context; it only depends
on the ``agent`` / ``tools`` / ``reporting`` layers.
"""

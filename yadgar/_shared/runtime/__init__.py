"""yadgar._shared.runtime — the recall RUNTIME shared by core and backend.

Houses the engine constellation bootstrap (state, lifecycle), the recall
pipeline, and the offload helper. Relocated here (folder-split #17, Car 1) so the
backend `/recall` endpoint imports its runner from `_shared` — closing the
former `backend -> server` seam. Pure relocation; behavior unchanged.
"""

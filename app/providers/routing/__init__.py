"""Provider routing layer for direct mail.

Prefers PostGrid for resource families it supports; falls back to Lob.
Snap-packs and booklets are Lob-only (PostGrid has no analog).
"""

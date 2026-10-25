"""One explicit SDR delivery format for video, RGB stills and JPEG covers."""

# Convert sample values before tagging. Merely assigning -color_range or
# -colorspace would reinterpret the existing pixels and alter their appearance.
# In particular JPEG decodes as full-range BT.601, unlike BT.709 screen captures.
SDR_FILTER = (
    "scale=iw:ih:out_color_matrix=bt709:out_range=tv,format=yuv420p,"
    "setparams=range=limited:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
)
SDR_ENCODER_ARGS = (
    "-color_range", "tv", "-colorspace", "bt709",
    "-color_primaries", "bt709", "-color_trc", "bt709",
)

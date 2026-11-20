"""Whether to start the HRTmon program that runs under ApolloOS.

The ROM HRTMon is a different thing. Its screen is not an Intuition screen, so
the remote desktop cannot see it, and Amigent does not enter it.
"""


def plan(os_name: str, version_text: str, tasks: str, on_path: bool) -> tuple[str, bool]:
    """The reply, and whether to run `Run >NIL: HRTmon`.

    The second value is never a request to enter the ROM monitor.
    """
    apollo = os_name == "ApolloOS" or "ApolloOS" in (version_text or "")
    if not apollo:
        return ("This machine is not running ApolloOS. HRTmon is not started. "
                "The ROM monitor stays unused.", False)
    if "hrtmon" in (tasks or "").lower():
        return ("HRTmon is already running. Use the screen and the keyboard. "
                "The ROM monitor stays unused.", False)
    if not on_path:
        return ("HRTmon is not on the command path. Put the HRTmon program that runs "
                "under the operating system where the Shell can find it, then ask again. "
                "The ROM monitor stays unused.", False)
    return ("HRTmon was started under ApolloOS. Use the screen and the keyboard. "
            "The ROM monitor stays unused.", True)

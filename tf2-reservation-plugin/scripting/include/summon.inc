#if defined _summon_included
    #endinput
#endif
#define _summon_included

/**
 * Called when the reservation owner requests a map change, for example with
 * !changemap or the owner map menu.
 *
 * Summon does not change the level itself; a handler such as Map Downloader
 * performs the change. The return value is ignored.
 *
 * @param client        Client index of the reservation owner.
 * @param mapName       Requested map name.
 * @return              Ignored.
 */
forward Action Summon_OnMapChangeRequested(int client, const char[] mapName);

public SharedPlugin __pl_summon =
{
    name = "summon",
    file = "summon.smx",
#if defined REQUIRE_PLUGIN
    required = 1,
#else
    required = 0,
#endif
};

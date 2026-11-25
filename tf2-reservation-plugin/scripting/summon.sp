/**
 * Summon - SourceMod Reservation Plugin
 *
 * Provides in-game reservation management for TF2 servers
 * Commands: !admin, !reservation, !res, !who, !end, !cancel,
 *           !changemap, !map, !config, !cfg, !restart, !command, !cmd, !rcon,
 *           !pause, !unpause
 */

#include <sourcemod>
#include <sdktools>
#include <ripext>

#pragma semicolon 1
#pragma newdecls required

#undef REQUIRE_PLUGIN
#include <logstf>
#include <demostf>
#define REQUIRE_PLUGIN

#define PLUGIN_VERSION "1.6.16"
#define PLUGIN_NAME "Summon"
#define PLAYER_UPDATE_INTERVAL 10.0
#define PLAYER_JOIN_REFRESH_DELAY 3.0
#define END_COUNTDOWN_SECONDS 10
#define OWNER_COMMAND_COOLDOWN 1.0
#define OWNER_COMMAND_CONFIG "configs/summon_owner_commands.cfg"
// Every log receiver gets a copy of each log line, so their number is capped.
#define MAX_OWNER_LOG_ADDRESSES 4
#define MAX_OWNER_JOIN_PASSWORD_LENGTH 64
// The game log goes to every log receiver, so players' addresses are taken
// out of it. The lines as the game wrote them are kept in this SourceMod log,
// which only the operator's log archive collects.
#define PLAYER_ADDRESS_LOG "logs/summon_player_addresses.log"
#define MASKED_IPV4 "0.0.0.0"
#define MAX_GAME_LOG_LINE 1024
#define MAX_OWNER_COMMAND_NAME 64
#define MAX_OWNER_COMMAND_LINE 512
#define MAX_OWNER_COMMAND_RESPONSE 4096
// Longer single-line output goes to the console, like multi-line output.
#define MAX_CHAT_REPLY_LENGTH 190
// Owners type "exec <config>" out of habit. It is never forwarded to the
// native exec: a config is a command script, so that would grant every
// command in every reachable .cfg. It loads through !config's checks instead.
#define OWNER_CONFIG_COMMAND "exec"
// TF2 clamps tv_delay to 0-120 seconds. Owners may set whole seconds in that
// range, and OnPluginStart makes the engine announce every change.
#define OWNER_TV_DELAY_COMMAND "tv_delay"
#define MAX_OWNER_TV_DELAY 120
#define OWNER_COMMAND_RESULT_MARKER "SUMMON_OWNER_COMMAND_RESULT"
#define OWNER_CHEATS_COMMAND "sv_cheats"
// Summon answers "maps <substring>" itself, from the maps on disk and the map
// catalog. See ReplyOwnerMapList.
#define OWNER_MAPS_COMMAND "maps"
// IsSafeMapName's 64 characters and the terminator.
#define MAX_MAP_NAME_LENGTH 65
#define MAX_MAP_CATALOG_SIZE 2048
#define MAP_CATALOG_RETRY_DELAY 60.0
// The sv_allow_point_servercommand value that lets no map entity or script
// run server commands.
#define POINT_SERVERCOMMAND_DISALLOW "disallow"
// The engine's own SourceTV map-change protection lasts tv_delay plus 3
// seconds after a round ends. The SourceTV wait keeps the same margin.
#define STV_WAIT_MARGIN 3.0
// The config mismatch check waits for joining players to finish loading, and
// for the "Loaded config" chat line that follows a config load.
#define CONFIG_MISMATCH_JOIN_DELAY 30.0
#define CONFIG_MISMATCH_LOAD_DELAY 1.0
// Auto config switches from 6v6 to Highlander once both teams have this many
// players. In 6v6 one team often has 7 or more for a while, with subs or
// before teams are picked, but both teams rarely do.
#define AUTO_HIGHLANDER_TEAM_SIZE 7
// CTFGameRules m_nGameType values. KOTH maps are control point maps too.
#define TF_GAMETYPE_CTF 1
#define TF_GAMETYPE_CP 2
#define TF_GAMETYPE_ESCORT 3
#define TF_TEAM_RED 2
#define TF_TEAM_BLUE 3

// ConVars - set by agent RCON at boot
ConVar g_cvOwnerSteamID;
ConVar g_cvOwnerName;
ConVar g_cvReservationNumber;
ConVar g_cvEndsAt;
ConVar g_cvBackendURL;
ConVar g_cvAPIKey;
// 1 when the reservation publishes the server's address. Without it, players
// connect through Steam's relays and the address stays hidden.
ConVar g_cvDirectConnect;

// Forward
GlobalForward g_fwdOnMapChangeRequested;

// State
bool g_bEndCountdownActive = false;
Handle g_hEndTimer = INVALID_HANDLE;
Handle g_hPlayerUpdateTimer = INVALID_HANDLE;
Handle g_hExpiryTimer = INVALID_HANDLE;
int g_iEndCountdown = 0;
bool g_bExpiryKickDone = false;
float g_fLastOwnerCommand = 0.0;
StringMap g_mOwnerCommandAllowlist;
int g_iOwnerCommandCount = 0;
// Log receivers added through owner commands, keyed by "ip:port".
StringMap g_mOwnerLogAddresses;
// Commands whose values the owner-command log leaves out.
char g_sOwnerSecretCommands[][] = {"sv_password", "sv_logsecret", "tv_password", "rcon_password"};
char g_sPlayerAddressLogPath[PLATFORM_MAX_PATH];
// Last config loaded through ExecuteConfig. Real configs need a league
// prefix, so "none" cannot collide with one.
char g_sCurrentConfig[64] = "none";

// Auto config: while g_bAutoConfig is set, Summon loads the league's config
// for each map's game mode itself, as 6v6 until g_bAutoHighlander is set.
// g_sAutoLeague is a league key of g_sAutoConfigs ("etf2l"), or empty until
// sm_config auto_<league> or the backend gives one. Any other config load
// turns Auto off; g_bAutoExecuting marks Auto's own.
bool g_bAutoConfig = false;
bool g_bAutoHighlander = false;
bool g_bAutoExecuting = false;
char g_sAutoLeague[16];

// Config mismatch warning. Both flags are cleared when the map or the config
// changes.
Handle g_hConfigMismatchTimer = INVALID_HANDLE;
bool g_bConfigMismatchChecked = false;
bool g_bConfigMismatchReadyChecked = false;

// SourceTV wait. g_fMatchLastLiveTime is 0.0 until a tournament match is seen
// live on the current map.
ConVar g_cvTournament;
ConVar g_cvTVDelayMapChangeProtect;
Handle g_hMatchStateTimer = INVALID_HANDLE;
float g_fMatchLastLiveTime = 0.0;
bool g_bSTVWaitActive = false;
Handle g_hSTVWaitTimer = INVALID_HANDLE;
int g_iSTVWaitStart = 0;
int g_iSTVWaitRemaining = 0;
char g_sSTVWaitMap[PLATFORM_MAX_PATH];

// Cheats safeguard. While g_bCheatsGuardActive is set, Summon holds
// sv_allow_point_servercommand at "disallow". g_sPointServerCommandRestore is
// the value it goes back to afterwards.
ConVar g_cvCheats;
ConVar g_cvPointServerCommand;
bool g_bCheatsGuardActive = false;
char g_sPointServerCommandRestore[32];

// The maps the reservation form offers, from the backend. Null until the
// first answer; a failed refresh keeps the previous list.
ArrayList g_aMapCatalog;
bool g_bMapCatalogRequestPending = false;
// Set once the catalog has been fetched, or found missing, since the map started.
bool g_bMapCatalogCurrent = false;
float g_fMapCatalogRetryAt = 0.0;

public Plugin myinfo = {
    name = PLUGIN_NAME,
    author = "",
    description = "TF2 server reservation management",
    version = PLUGIN_VERSION,
    url = ""
};

public APLRes AskPluginLoad2(Handle myself, bool late, char[] error, int err_max)
{
    g_fwdOnMapChangeRequested = new GlobalForward(
        "Summon_OnMapChangeRequested",
        ET_Event,
        Param_Cell,       // int client
        Param_String      // const char[] mapName
    );

    RegPluginLibrary("summon");
    return APLRes_Success;
}

public void OnPluginStart()
{
    // Create ConVars
    g_cvOwnerSteamID = CreateConVar("sm_reserve_owner", "", "Steam ID of reservation owner", FCVAR_PROTECTED);
    g_cvOwnerName = CreateConVar("sm_reserve_owner_name", "", "Display name of reservation owner", FCVAR_PROTECTED);
    g_cvReservationNumber = CreateConVar("sm_reserve_number", "0", "Reservation number", FCVAR_PROTECTED);
    g_cvEndsAt = CreateConVar("sm_reserve_ends_at", "0", "Unix timestamp when reservation ends", FCVAR_PROTECTED);
    g_cvBackendURL = CreateConVar("sm_reserve_backend_url", "", "Backend API URL", FCVAR_PROTECTED);
    g_cvAPIKey = CreateConVar("sm_reserve_api_key", "", "Internal API key", FCVAR_PROTECTED);
    g_cvDirectConnect = CreateConVar(
        "sm_reserve_direct_connect", "0",
        "1 when the reservation publishes the server's address",
        FCVAR_PROTECTED, true, 0.0, true, 1.0
    );

    // Register chat commands
    RegConsoleCmd("sm_reservation", Command_Reservation, "Show reservation info");
    RegConsoleCmd("sm_res", Command_Reservation, "Alias of sm_reservation");
    RegConsoleCmd("sm_pause", Command_Pause, "Pause the game");
    RegConsoleCmd("sm_unpause", Command_Unpause, "Unpause the game");
    RegConsoleCmd("sm_end", Command_End, "End the reservation");
    RegConsoleCmd("sm_cancel", Command_Cancel, "Cancel pending reservation end");
    RegConsoleCmd("sm_changemap", Command_Map, "Change the map (owner only)");
    RegConsoleCmd("sm_config", Command_Config, "Show the current config, or load one (owner only)");
    RegConsoleCmd("sm_cfg", Command_Config, "Alias of sm_config");
    RegConsoleCmd("sm_restart", Command_Restart, "Restart tournament/game/round (owner only)");
    RegAdminCmd(
        "sm_summon_reload_owner_commands",
        Command_ReloadOwnerCommands,
        ADMFLAG_CONFIG,
        "Reload Summon's reservation-owner command allowlist"
    );

    LoadOwnerCommandAllowlist();
    g_mOwnerLogAddresses = new StringMap();

    BuildPath(Path_SM, g_sPlayerAddressLogPath, sizeof(g_sPlayerAddressLogPath), PLAYER_ADDRESS_LOG);
    AddGameLogHook(GameLog_MaskPlayerAddresses);

    // tv_delay is not a notify ConVar, so the engine tells nobody when it
    // changes, and players cannot see who watches SourceTV. With the flag,
    // every change is announced in chat, whether it comes from an owner
    // command or a config, so a shorter delay cannot go unnoticed.
    ConVar tvDelay = FindConVar("tv_delay");
    if (tvDelay == null)
    {
        LogError("[summon] tv_delay not found; SourceTV delay changes will not be announced");
    }
    else
    {
        tvDelay.Flags |= FCVAR_NOTIFY;
    }

    g_cvTournament = FindConVar("mp_tournament");

    if (!HookEventEx("tournament_stateupdate", Event_TournamentStateUpdate))
    {
        LogError("[summon] tournament_stateupdate not found; config mismatches will not be reported at ready-up");
    }

    // League configs turn on the engine's map-change protection. It refuses
    // every map change for tv_delay + 3 seconds after a round ends, even when
    // nobody watches SourceTV. Summon's SourceTV wait replaces it.
    g_cvTVDelayMapChangeProtect = FindConVar("tv_delaymapchange_protect");
    if (g_cvTVDelayMapChangeProtect == null)
    {
        LogError("[summon] tv_delaymapchange_protect not found; configs may block map changes after each round");
    }
    else
    {
        g_cvTVDelayMapChangeProtect.AddChangeHook(ConVar_TVDelayMapChangeProtect);
        if (!IsRGLQoLLoaded())
            DisableTVDelayMapChangeProtect();
    }

    // With cheats on, every player can create entities and fire their inputs.
    // A point_servercommand entity, or a script that calls
    // SendToServerConsole, then runs any server command for them, including
    // the ones owners are not allowed. sv_allow_point_servercommand gates
    // both, so Summon keeps them off while cheats are on.
    g_cvCheats = FindConVar(OWNER_CHEATS_COMMAND);
    g_cvPointServerCommand = FindConVar("sv_allow_point_servercommand");
    if (!IsCheatsGuardAvailable())
    {
        LogError("[summon] sv_cheats or sv_allow_point_servercommand not found; owners cannot change sv_cheats");
    }
    else
    {
        g_cvCheats.AddChangeHook(ConVar_Cheats);
        g_cvPointServerCommand.AddChangeHook(ConVar_PointServerCommand);
        if (g_cvCheats.BoolValue)
            StartCheatsGuard();
    }

    if (CommandExists("sm_command"))
    {
        // A shared Reg*Cmd name can execute multiple plugin callbacks. Do not
        // offer the fallback alias if another plugin already owns it.
        LogError("[summon] sm_command is already registered; Summon's !command entry point disabled");
    }
    else
    {
        RegConsoleCmd("sm_command", Command_OwnerServerCommand, "Run an allowed server command (owner only)");
    }

    if (CommandExists("sm_cmd"))
    {
        LogError("[summon] sm_cmd is already registered; owner command alias disabled");
    }
    else
    {
        RegConsoleCmd("sm_cmd", Command_OwnerServerCommand, "Alias of sm_command");
    }

    // Preserve the familiar !rcon syntax without granting owners access to
    // SourceMod's unrestricted sm_rcon implementation.
    if (!AddCommandListener(Listener_OwnerRcon, "sm_rcon"))
    {
        LogError("[summon] Failed to register sm_rcon command listener; owners can still use !command or !cmd");
    }

    // These names are registered by SourceMod. Listen instead of registering
    // duplicate callbacks so callers with stock access retain stock behavior.
    if (!AddCommandListener(Listener_OwnerAdmin, "sm_admin"))
    {
        LogError("[summon] Failed to register sm_admin command listener; owner menu alias disabled");
    }
    if (!AddCommandListener(Listener_ReservationWho, "sm_who"))
    {
        LogError("[summon] Failed to register sm_who command listener; reservation info alias disabled");
    }
    if (!AddCommandListener(Listener_OwnerMap, "sm_map"))
    {
        LogError("[summon] Failed to register sm_map command listener; owners must use !changemap");
    }

    // Register RCON commands (called by agent)
    RegServerCmd("sm_reservation_warning", Command_ReservationWarning);
    RegServerCmd("sm_reservation_ending", Command_ReservationEnding);
    RegServerCmd(
        "sm_summon_owner_command",
        Command_ServerOwnerCommand,
        "Run one restricted reservation-owner command for the web console"
    );
    RegServerCmd(
        "sm_summon_rcon_command",
        Command_ServerRconCommand,
        "Run one restricted reservation-owner command for the agent's RCON gateway, without the cooldown"
    );

    // changelevel only runs as a server command: from the console, RCON,
    // owner commands, and ForceChangeLevel.
    RegServerCmd("changelevel", Command_Changelevel);
    RegServerCmd(
        "stopchangelevel",
        Command_StopChangelevel,
        "Cancel a map change that is waiting for SourceTV viewers"
    );
    LogMessage("[summon] Plugin loaded v%s", PLUGIN_VERSION);
}

public void OnAllPluginsLoaded()
{
    if (!CommandExists("sm_rcon"))
    {
        LogError("[summon] sm_rcon is not registered; owners must use !command or !cmd");
    }
    if (!CommandExists("sm_admin"))
    {
        LogError("[summon] sm_admin is not registered; owner menu alias unavailable");
    }
    if (!CommandExists("sm_who"))
    {
        LogError("[summon] sm_who is not registered; reservation info alias unavailable");
    }
    if (!CommandExists("sm_map"))
    {
        LogError("[summon] sm_map is not registered; owners must use !changemap");
    }
}

// ============================================================================
// Player Tracking
// ============================================================================

public void OnMapEnd()
{
    // TIMER_FLAG_NO_MAPCHANGE auto-kills these timers on map change,
    // so reset handles to avoid invalid-handle errors in OnMapStart.
    g_hPlayerUpdateTimer = INVALID_HANDLE;
    g_hExpiryTimer = INVALID_HANDLE;
    g_hMatchStateTimer = INVALID_HANDLE;
    g_hSTVWaitTimer = INVALID_HANDLE;
    g_bSTVWaitActive = false;
    g_hConfigMismatchTimer = INVALID_HANDLE;
}

public void OnMapInit(const char[] mapName)
{
    // The previous map's entities and scripts are gone and this map's have
    // not been created yet.
    if (g_bCheatsGuardActive && !g_cvCheats.BoolValue)
        StopCheatsGuard();
}

public void OnMapStart()
{
    // Start periodic player updates for live ping/connect times.
    if (g_hPlayerUpdateTimer != INVALID_HANDLE)
    {
        KillTimer(g_hPlayerUpdateTimer);
    }
    g_hPlayerUpdateTimer = CreateTimer(PLAYER_UPDATE_INTERVAL, Timer_PeriodicPlayerUpdate, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);

    // Start expiry countdown timer (1-second tick)
    if (g_hExpiryTimer != INVALID_HANDLE)
    {
        KillTimer(g_hExpiryTimer);
    }
    g_bExpiryKickDone = false;
    g_hExpiryTimer = CreateTimer(1.0, Timer_ExpiryCheck, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);

    if (g_hMatchStateTimer != INVALID_HANDLE)
    {
        KillTimer(g_hMatchStateTimer);
    }
    g_fMatchLastLiveTime = 0.0;
    g_hMatchStateTimer = CreateTimer(1.0, Timer_MatchState, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);

    // The loaded config may not suit the new map. The check runs once
    // players have joined.
    g_bConfigMismatchChecked = false;
    g_bConfigMismatchReadyChecked = false;

    // Report the new map before players finish loading it, so the MOTD they
    // are shown already has it.
    SendPlayerUpdate();

    // Admins may have changed the catalog since the last map.
    g_bMapCatalogCurrent = false;
    RequestMapCatalog();
}

public void OnConfigsExecuted()
{
    // After the map's own configs, which an RGL config's servercfgfile adds
    // to, so Auto's config is the one that stays.
    if (g_bAutoConfig)
        ApplyAutoConfig(false);
}

public void OnClientPostAdminCheck(int client)
{
    if (IsFakeClient(client))
        return;

    SendPlayerUpdate();
    CreateTimer(PLAYER_JOIN_REFRESH_DELAY, Timer_SendPlayerUpdate, _, TIMER_FLAG_NO_MAPCHANGE);

    // Players rejoin after every map change, so this also covers a config
    // that no longer suits the map, and one loaded while the server was empty.
    if (!g_bConfigMismatchChecked && g_hConfigMismatchTimer == INVALID_HANDLE)
        ScheduleConfigMismatchCheck(CONFIG_MISMATCH_JOIN_DELAY);
}

public void OnClientDisconnect(int client)
{
    if (IsFakeClient(client))
        return;

    // Delay so disconnecting player is already gone from the count
    CreateTimer(0.1, Timer_SendPlayerUpdate);
}

public Action Timer_SendPlayerUpdate(Handle timer)
{
    SendPlayerUpdate();
    return Plugin_Stop;
}

public Action Timer_PeriodicPlayerUpdate(Handle timer)
{
    SendPlayerUpdate();
    // The agent sets the backend ConVars after the first map has started, so
    // the first request is usually made from here.
    RequestMapCatalog();
    return Plugin_Continue;
}

int GetDisplayPing(int client)
{
    float latency = GetClientAvgLatency(client, NetFlow_Both);
    if (latency < 0.0)
    {
        latency = GetClientLatency(client, NetFlow_Both);
    }

    if (latency < 0.0)
    {
        return -1;
    }

    int ping = RoundToNearest(latency * 1000.0);
    if (ping < 0)
    {
        return -1;
    }

    return ping;
}

void SendPlayerUpdate()
{
    char backendURL[256];
    char apiKey[64];

    g_cvBackendURL.GetString(backendURL, sizeof(backendURL));
    g_cvAPIKey.GetString(apiKey, sizeof(apiKey));

    int reservationNumber = g_cvReservationNumber.IntValue;

    if (strlen(backendURL) == 0 || strlen(apiKey) == 0 || reservationNumber == 0)
        return;

    // Build player list
    JSONArray playersArr = new JSONArray();
    int playerCount = 0;

    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i) || IsFakeClient(i))
            continue;

        char name[64];
        char steamId[32];

        GetClientName(i, name, sizeof(name));
        if (!GetClientAuthId(i, AuthId_SteamID64, steamId, sizeof(steamId)))
            continue;

        float connectTime = GetClientTime(i);
        int ping = GetDisplayPing(i);

        JSONObject player = new JSONObject();
        player.SetString("name", name);
        player.SetString("steam_id", steamId);
        player.SetInt("connect_time", RoundToNearest(connectTime));
        player.SetInt("ping", ping);
        playersArr.Push(player);
        delete player;

        playerCount++;
    }

    char mapName[PLATFORM_MAX_PATH];
    GetCurrentMap(mapName, sizeof(mapName));

    // Build payload
    JSONObject body = new JSONObject();
    body.SetInt("player_count", playerCount);
    body.Set("players", playersArr);
    delete playersArr;
    body.SetString("map", mapName);
    // The backend stores no config as an empty string.
    if (StrEqual(g_sCurrentConfig, "none"))
        body.SetString("config_file", "");
    else
        body.SetString("config_file", g_sCurrentConfig);
    body.SetBool("auto_config", g_bAutoConfig);

    // Send HTTP POST
    char url[512];
    Format(url, sizeof(url), "%s/internal/reservations/%d/players", backendURL, reservationNumber);

    HTTPRequest request = new HTTPRequest(url);
    request.SetHeader("X-API-Key", apiKey);
    request.SetHeader("Content-Type", "application/json");
    request.Post(body, OnPlayerUpdateResponse);
    delete body;
}

public void OnPlayerUpdateResponse(HTTPResponse response, any data, const char[] error)
{
    if (strlen(error) > 0)
    {
        LogError("[summon] Failed to send player update: %s", error);
        return;
    }

    if (response.Status != HTTPStatus_OK)
    {
        LogError("[summon] Backend returned HTTP %d for player update", response.Status);
        return;
    }

    // The backend names the league of the server's region, so owners can
    // turn Auto on in game. A league from sm_config auto_<league> stays.
    // The response frees its JSON.
    JSONObject reply = view_as<JSONObject>(response.Data);
    if (reply == null || g_sAutoLeague[0] != '\0' || !reply.HasKey("auto_league"))
        return;

    char league[sizeof(g_sAutoLeague)];
    if (reply.GetString("auto_league", league, sizeof(league)) && IsAutoConfigLeague(league))
        strcopy(g_sAutoLeague, sizeof(g_sAutoLeague), league);
}

// ============================================================================
// Map Catalog
// ============================================================================

// The request runs in the background, so it never delays the server.
void RequestMapCatalog()
{
    if (g_bMapCatalogRequestPending
        || g_bMapCatalogCurrent
        || GetEngineTime() < g_fMapCatalogRetryAt)
        return;

    char backendURL[256];
    char apiKey[64];
    g_cvBackendURL.GetString(backendURL, sizeof(backendURL));
    g_cvAPIKey.GetString(apiKey, sizeof(apiKey));
    int reservationNumber = g_cvReservationNumber.IntValue;
    if (strlen(backendURL) == 0 || strlen(apiKey) == 0 || reservationNumber == 0)
        return;

    char url[512];
    Format(url, sizeof(url), "%s/internal/reservations/%d/maps", backendURL, reservationNumber);

    HTTPRequest request = new HTTPRequest(url);
    request.SetHeader("X-API-Key", apiKey);
    request.Get(OnMapCatalogResponse);
    g_bMapCatalogRequestPending = true;
}

public void OnMapCatalogResponse(HTTPResponse response, any data, const char[] error)
{
    g_bMapCatalogRequestPending = false;
    // Set first, so a response that cannot be read is also retried later.
    g_fMapCatalogRetryAt = GetEngineTime() + MAP_CATALOG_RETRY_DELAY;

    if (strlen(error) > 0)
    {
        LogError("[summon] Failed to fetch the map catalog: %s", error);
        return;
    }

    // A backend older than the catalog. Try again after the next map change.
    if (response.Status == HTTPStatus_NotFound)
    {
        g_bMapCatalogCurrent = true;
        return;
    }

    if (response.Status != HTTPStatus_OK)
    {
        LogError("[summon] Backend returned HTTP %d for the map catalog", response.Status);
        return;
    }

    // The response frees its JSON; values taken from it must be freed here.
    JSONObject reply = view_as<JSONObject>(response.Data);
    if (reply == null || !reply.HasKey("maps"))
    {
        LogError("[summon] The map catalog response has no map list");
        return;
    }

    JSONArray maps = view_as<JSONArray>(reply.Get("maps"));
    ArrayList catalog = new ArrayList(ByteCountToCells(MAX_MAP_NAME_LENGTH));
    // Read into a larger buffer, so a longer name fails IsSafeMapName instead
    // of being cut to a valid one.
    char name[PLATFORM_MAX_PATH];
    int count = maps.Length;
    for (int i = 0; i < count && catalog.Length < MAX_MAP_CATALOG_SIZE; i++)
    {
        if (maps.GetString(i, name, sizeof(name)) && IsSafeMapName(name))
            catalog.PushString(name);
    }
    delete maps;

    delete g_aMapCatalog;
    g_aMapCatalog = catalog;
    g_bMapCatalogCurrent = true;
}

// The engine's maps command lists only the maps on disk, and this server
// keeps few: it downloads a map from FastDL when it changes to one. Lobby
// sites check a server with "maps *" before they use it, so the reply adds
// the maps the reservation form offers, every one of which the server can
// download. Every line is in the engine's format, even for a map that is not
// on disk yet, because that is what the sites read. Client 0 is the server
// console, which RCON reads.
void ReplyOwnerMapList(int client, const char[] substring)
{
    // The engine prints nothing for an empty substring.
    if (substring[0] == '\0')
        return;

    ArrayList names = CollectOwnerMapNames(substring);
    if (names.Length > 0)
        PrintOwnerOutputLine(client, "-------------");

    char name[MAX_MAP_NAME_LENGTH];
    char line[MAX_MAP_NAME_LENGTH + 32];
    for (int i = 0; i < names.Length; i++)
    {
        names.GetString(i, name, sizeof(name));
        Format(line, sizeof(line), "PENDING:   (fs) %s", name);
        PrintOwnerOutputLine(client, line);
    }
    delete names;
}

void PrintOwnerOutputLine(int client, const char[] line)
{
    if (client == 0)
        PrintToServer("%s", line);
    else if (IsClientInGame(client))
        PrintToConsole(client, "%s", line);
}

// The maps on disk and in the catalog whose names contain the substring,
// ignoring case, or every one for "*" as in the engine. Sorted, without
// duplicates.
ArrayList CollectOwnerMapNames(const char[] substring)
{
    ArrayList names = new ArrayList(ByteCountToCells(MAX_MAP_NAME_LENGTH));
    StringMap seen = new StringMap();
    bool everyMap = substring[0] == '*';

    // Through the game's file system, so maps in every search path count, as
    // they do for the engine.
    DirectoryListing directory = OpenDirectory("maps", true);
    if (directory != null)
    {
        char entry[PLATFORM_MAX_PATH];
        FileType type;
        while (directory.GetNext(entry, sizeof(entry), type))
        {
            int length = strlen(entry);
            if (type != FileType_File || length <= 4 || !StrEqual(entry[length - 4], ".bsp"))
                continue;
            entry[length - 4] = '\0';
            AddOwnerMapName(names, seen, entry, substring, everyMap);
        }
        delete directory;
    }

    if (g_aMapCatalog != null)
    {
        char name[MAX_MAP_NAME_LENGTH];
        for (int i = 0; i < g_aMapCatalog.Length; i++)
        {
            g_aMapCatalog.GetString(i, name, sizeof(name));
            AddOwnerMapName(names, seen, name, substring, everyMap);
        }
    }

    delete seen;
    names.SortCustom(SortMapNames);
    return names;
}

void AddOwnerMapName(
    ArrayList names,
    StringMap seen,
    const char[] name,
    const char[] substring,
    bool everyMap
)
{
    if (!IsSafeMapName(name))
        return;
    if (!everyMap && StrContains(name, substring, false) == -1)
        return;
    // SetValue refuses a name that is already there.
    if (seen.SetValue(name, true, false))
        names.PushString(name);
}

public int SortMapNames(int index1, int index2, Handle array, Handle hndl)
{
    ArrayList names = view_as<ArrayList>(array);
    char first[MAX_MAP_NAME_LENGTH];
    char second[MAX_MAP_NAME_LENGTH];
    names.GetString(index1, first, sizeof(first));
    names.GetString(index2, second, sizeof(second));
    return strcmp(first, second, false);
}

bool IsOwnerMapListing(const char[] operation, int valueArgumentCount)
{
    // Without a substring, the engine prints its usage.
    return valueArgumentCount == 1 && StrEqual(operation, OWNER_MAPS_COMMAND);
}

// ============================================================================
// Helper Functions
// ============================================================================

bool IsOwner(int client)
{
    if (client < 1 || client > MaxClients || !IsClientConnected(client) || !IsClientInGame(client) || IsFakeClient(client))
        return false;

    char clientSteamID[32];
    char ownerSteamID[32];

    if (!GetClientAuthId(client, AuthId_SteamID64, clientSteamID, sizeof(clientSteamID)))
    {
        return false;
    }

    g_cvOwnerSteamID.GetString(ownerSteamID, sizeof(ownerSteamID));

    if (ownerSteamID[0] == '\0')
        return false;

    return StrEqual(clientSteamID, ownerSteamID, false);
}

bool ReservationAllowsOwnerServerCommands()
{
    return g_cvReservationNumber.IntValue > 0
        && !g_bExpiryKickDone
        && GetTimeRemaining() > 0;
}

int GetTimeRemaining()
{
    int endsAt = g_cvEndsAt.IntValue;
    int now = GetTime();
    return endsAt - now;
}

void FormatTimeRemaining(int seconds, char[] buffer, int bufferSize)
{
    if (seconds <= 0)
    {
        Format(buffer, bufferSize, "expired");
        return;
    }

    int hours = seconds / 3600;
    int minutes = (seconds % 3600) / 60;

    if (hours > 0)
    {
        Format(buffer, bufferSize, "%d hour%s %d minute%s",
            hours, hours == 1 ? "" : "s",
            minutes, minutes == 1 ? "" : "s");
    }
    else
    {
        Format(buffer, bufferSize, "%d minute%s", minutes, minutes == 1 ? "" : "s");
    }
}

bool NormalizeOwnerCommandName(char[] command)
{
    int length = strlen(command);
    if (length <= 0 || length >= MAX_OWNER_COMMAND_NAME)
        return false;

    for (int i = 0; i < length; i++)
    {
        int character = command[i];
        bool isLetter = (character >= 'a' && character <= 'z')
            || (character >= 'A' && character <= 'Z');
        bool isDigit = character >= '0' && character <= '9';

        // Source/SourceMod command and CVAR identifiers use this character
        // set. Keeping config keys to one token makes exact matching
        // unambiguous and prevents the allowlist itself becoming a command
        // line.
        if (!isLetter && !isDigit && character != '_')
            return false;

        command[i] = CharToLower(character);
    }

    return true;
}

void ReplaceOwnerCommandAllowlist(StringMap replacement)
{
    delete g_mOwnerCommandAllowlist;
    g_mOwnerCommandAllowlist = replacement;
    g_iOwnerCommandCount = replacement.Size;
}

bool LoadOwnerCommandAllowlist()
{
    char path[PLATFORM_MAX_PATH];
    BuildPath(Path_SM, path, sizeof(path), OWNER_COMMAND_CONFIG);

    StringMap replacement = new StringMap();
    KeyValues config = new KeyValues("SummonOwnerCommands");
    if (!config.ImportFromFile(path))
    {
        delete config;
        ReplaceOwnerCommandAllowlist(replacement);
        LogError("[summon] Could not read owner command allowlist at %s; owner command access disabled", path);
        return false;
    }

    char rootName[64];
    config.GetSectionName(rootName, sizeof(rootName));
    if (!StrEqual(rootName, "SummonOwnerCommands"))
    {
        delete config;
        ReplaceOwnerCommandAllowlist(replacement);
        LogError("[summon] Invalid root in %s; expected SummonOwnerCommands and disabled owner command access", path);
        return false;
    }

    bool valid = true;
    if (config.GotoFirstSubKey())
    {
        do
        {
            char command[256];
            if (!config.GetSectionName(command, sizeof(command)) || !NormalizeOwnerCommandName(command))
            {
                valid = false;
                LogError("[summon] Invalid command name in %s", path);
                break;
            }

            if (!replacement.SetValue(command, 1, false))
            {
                valid = false;
                LogError("[summon] Duplicate command name in %s: %s", path, command);
                break;
            }
        }
        while (config.GotoNextKey());
    }

    delete config;

    if (!valid || replacement.Size == 0)
    {
        delete replacement;
        replacement = new StringMap();
        ReplaceOwnerCommandAllowlist(replacement);
        LogError("[summon] Owner command allowlist is invalid or empty; owner command access disabled");
        return false;
    }

    ReplaceOwnerCommandAllowlist(replacement);
    LogMessage("[summon] Loaded %d reservation-owner commands from %s", g_iOwnerCommandCount, path);
    return true;
}

public Action Command_ReloadOwnerCommands(int client, int args)
{
    if (LoadOwnerCommandAllowlist())
    {
        ReplyToCommand(client, "[SM] Loaded %d reservation-owner commands.", g_iOwnerCommandCount);
    }
    else
    {
        ReplyToCommand(client, "[SM] Owner command allowlist was not loaded; access is disabled. See the SourceMod log.");
    }

    return Plugin_Handled;
}

bool IsSafeOwnerCommandLine(const char[] commandLine)
{
    for (int i = 0; commandLine[i] != '\0'; i++)
    {
        int character = commandLine[i];

        // The Source command buffer treats semicolons and line breaks as
        // command separators. Reject all controls as well so one allowlisted
        // command can never be used to append a second command or forge logs.
        if (character == ';' || (character > 0 && character < 32) || character == 127)
            return false;
    }

    return true;
}

bool IsSafeTournamentWhitelistPath(const char[] path)
{
    int length = strlen(path);

    // An empty value removes the active whitelist. Non-empty values must stay
    // inside cfg, use a plain portable filename, and already exist in the
    // game's filesystem search path.
    if (length == 0)
        return true;

    if (length < 9
        || length >= PLATFORM_MAX_PATH
        || StrContains(path, "cfg/", false) != 0
        || StrContains(path, "..") != -1
        || StrContains(path, "\\") != -1
        || !StrEqual(path[length - 4], ".txt", false))
    {
        return false;
    }

    for (int i = 0; i < length; i++)
    {
        int character = path[i];
        bool isLetter = (character >= 'a' && character <= 'z')
            || (character >= 'A' && character <= 'Z');
        bool isDigit = character >= '0' && character <= '9';

        if (!isLetter && !isDigit
            && character != '_'
            && character != '-'
            && character != '.'
            && character != '/')
        {
            return false;
        }
    }

    return FileExists(path, true);
}

bool IsSafeMapName(const char[] mapName)
{
    int length = strlen(mapName);
    if (length <= 0 || length > 64)
        return false;

    for (int i = 0; i < length; i++)
    {
        int character = mapName[i];
        bool isLetter = (character >= 'a' && character <= 'z')
            || (character >= 'A' && character <= 'Z');
        bool isDigit = character >= '0' && character <= '9';

        if (!isLetter && !isDigit && character != '_' && character != '-')
            return false;
    }

    return true;
}

bool GetOwnerConfigName(const char[] value, char[] name, int nameSize)
{
    // Accept the "exec name.cfg" spelling, but only as a plain identifier so
    // the name cannot select a path or a different file after truncation.
    int length = strlen(value);
    if (length > 4 && StrEqual(value[length - 4], ".cfg", false))
        length -= 4;

    if (length <= 0 || length >= nameSize)
        return false;

    for (int i = 0; i < length; i++)
    {
        int character = value[i];
        bool isLetter = (character >= 'a' && character <= 'z')
            || (character >= 'A' && character <= 'Z');
        bool isDigit = character >= '0' && character <= '9';

        if (!isLetter && !isDigit && character != '_')
            return false;
    }

    strcopy(name, length + 1, value);
    return true;
}

bool IsAllowedTVDelay(const char[] value)
{
    // Whole seconds only. The engine reads any other text as 0, so a typo
    // would otherwise remove the delay.
    int length = strlen(value);
    if (length <= 0 || length > 3)
        return false;

    for (int i = 0; i < length; i++)
    {
        if (value[i] < '0' || value[i] > '9')
            return false;
    }

    return StringToInt(value) <= MAX_OWNER_TV_DELAY;
}

// Copies the text after the command name, without the one pair of quotes the
// engine strips from a ConVar value. commandLine is the trimmed raw line.
void GetOwnerCommandRawValue(const char[] commandLine, char[] value, int valueSize)
{
    int index = 0;
    while (commandLine[index] != '\0' && commandLine[index] != ' ')
        index++;
    while (commandLine[index] == ' ')
        index++;

    strcopy(value, valueSize, commandLine[index]);
    TrimString(value);

    int length = strlen(value);
    if (length >= 2 && value[0] == '"' && value[length - 1] == '"')
    {
        value[length - 1] = '\0';
        strcopy(value, valueSize, value[1]);
        TrimString(value);
    }
}

bool IsDecimalNumber(const char[] value, int maxDigits)
{
    int length = strlen(value);
    if (length <= 0 || length > maxDigits)
        return false;

    for (int i = 0; i < length; i++)
    {
        if (value[i] < '0' || value[i] > '9')
            return false;
    }

    return true;
}

// Parses "a.b.c.d:port" strictly and writes it back in canonical form. Host
// names are refused: the engine would resolve them on the game thread.
bool ParseLogAddress(const char[] value, char[] canonical, int canonicalSize, int octets[4])
{
    char halves[2][32];
    if (ExplodeString(value, ":", halves, sizeof(halves), sizeof(halves[])) != 2)
        return false;

    char parts[4][8];
    if (CharCount(value, ':') != 1
        || CharCount(halves[0], '.') != 3
        || ExplodeString(halves[0], ".", parts, sizeof(parts), sizeof(parts[])) != 4)
    {
        return false;
    }

    for (int i = 0; i < 4; i++)
    {
        if (!IsDecimalNumber(parts[i], 3))
            return false;
        octets[i] = StringToInt(parts[i]);
        if (octets[i] > 255)
            return false;
    }

    if (!IsDecimalNumber(halves[1], 5))
        return false;
    int port = StringToInt(halves[1]);
    if (port < 1 || port > 65535)
        return false;

    Format(
        canonical, canonicalSize, "%d.%d.%d.%d:%d",
        octets[0], octets[1], octets[2], octets[3], port
    );
    return true;
}

int CharCount(const char[] value, int character)
{
    int count = 0;
    for (int i = 0; value[i] != '\0'; i++)
    {
        if (value[i] == character)
            count++;
    }
    return count;
}

// Logs may only be sent to addresses on the public internet, never to the
// host, its private networks, or multicast and reserved ranges.
bool IsPublicIPv4(const int octets[4])
{
    int a = octets[0];
    int b = octets[1];

    if (a == 0 || a == 10 || a == 127 || a >= 224)
        return false;
    if (a == 100 && b >= 64 && b <= 127)
        return false;
    if (a == 169 && b == 254)
        return false;
    if (a == 172 && b >= 16 && b <= 31)
        return false;
    if (a == 192 && b == 168)
        return false;
    if (a == 192 && b == 0 && octets[2] == 0)
        return false;
    if (a == 198 && (b == 18 || b == 19))
        return false;

    return true;
}

bool IsPlainIdentifier(const char[] value, int maxLength)
{
    int length = strlen(value);
    if (length <= 0 || length > maxLength)
        return false;

    for (int i = 0; i < length; i++)
    {
        int character = value[i];
        bool isLetter = (character >= 'a' && character <= 'z')
            || (character >= 'A' && character <= 'Z');
        bool isDigit = character >= '0' && character <= '9';

        if (!isLetter && !isDigit && character != '_')
            return false;
    }

    return true;
}

// Argument rules for owner commands, whichever way they arrive. A command
// with no rule here accepts every native argument form. On refusal, message
// says what the command accepts. commandLine is the trimmed raw line.
bool AreOwnerCommandArgumentsAllowed(
    const char[] operation,
    int valueArgumentCount,
    const char[] firstValue,
    const char[] commandLine,
    char[] message,
    int messageSize
)
{
    if (StrEqual(operation, "changelevel"))
    {
        if (valueArgumentCount == 1 && IsSafeMapName(firstValue))
            return true;
        strcopy(message, messageSize, "Changelevel requires exactly one map name containing only letters, numbers, underscores, or hyphens.");
        return false;
    }

    // Reading the current delay takes no value argument.
    if (StrEqual(operation, OWNER_TV_DELAY_COMMAND))
    {
        if (valueArgumentCount == 0
            || (valueArgumentCount == 1 && IsAllowedTVDelay(firstValue)))
            return true;
        strcopy(message, messageSize, "tv_delay takes one whole number of seconds from 0 to 120.");
        return false;
    }

    if (StrEqual(operation, OWNER_CONFIG_COMMAND))
    {
        char configName[64];
        if (valueArgumentCount == 1
            && GetOwnerConfigName(firstValue, configName, sizeof(configName))
            && IsAllowedConfig(configName))
            return true;
        strcopy(message, messageSize, "Unknown config. Use !config to see available options.");
        return false;
    }

    // Reading the current value takes no value argument. Setting it takes one
    // path argument; accepting more would make the native parsing ambiguous.
    if (StrEqual(operation, "mp_tournament_whitelist"))
    {
        if (valueArgumentCount == 0
            || (valueArgumentCount == 1 && IsSafeTournamentWhitelistPath(firstValue)))
            return true;
        strcopy(message, messageSize, "That command's arguments are not allowed. Tournament whitelists must be existing cfg/*.txt files.");
        return false;
    }

    // maps lists map names and changes nothing. The engine reads only its
    // first argument, so a second one is refused rather than ignored.
    if (StrEqual(operation, OWNER_MAPS_COMMAND))
    {
        if (valueArgumentCount <= 1)
            return true;
        strcopy(message, messageSize, "maps takes one part of a map name, or * for every map.");
        return false;
    }

    // These take no arguments: most report state, and logaddress_delall
    // removes every log receiver. hostname, maxplayers, and the tv_ entries
    // can be read but not changed.
    if (StrEqual(operation, "status")
        || StrEqual(operation, "users")
        || StrEqual(operation, "hostname")
        || StrEqual(operation, "maxplayers")
        || StrEqual(operation, "logaddress_list")
        || StrEqual(operation, "logaddress_delall")
        || StrEqual(operation, "tv_port")
        || StrEqual(operation, "tv_password")
        || StrEqual(operation, "tv_status"))
    {
        if (valueArgumentCount == 0)
            return true;
        Format(message, messageSize, "%s takes no arguments here.", operation);
        return false;
    }

    if (StrEqual(operation, "kickid"))
    {
        // By the user ID that status lists, or by SteamID. SourceTV must stay:
        // it records the demo and carries the broadcast. It has no SteamID,
        // so only its user ID needs checking.
        if (valueArgumentCount >= 1)
        {
            if (IsDecimalNumber(firstValue, 5))
            {
                int target = GetClientOfUserId(StringToInt(firstValue));
                if (target == 0 || (!IsClientSourceTV(target) && !IsClientReplay(target)))
                    return true;
            }
            else
            {
                char rawValue[MAX_OWNER_COMMAND_LINE];
                GetOwnerCommandRawValue(commandLine, rawValue, sizeof(rawValue));
                if (StrContains(rawValue, "STEAM_", false) == 0 || StrContains(rawValue, "[U:", false) == 0)
                    return true;
            }
        }
        strcopy(message, messageSize, "kickid takes a user ID from status or a SteamID, then an optional message. SourceTV cannot be kicked.");
        return false;
    }

    if (StrEqual(operation, "say"))
    {
        if (valueArgumentCount >= 1)
            return true;
        strcopy(message, messageSize, "say needs a message.");
        return false;
    }

    if (StrEqual(operation, "sv_password"))
    {
        if (valueArgumentCount == 0)
            return true;

        // An empty password would open the server to everyone.
        char password[MAX_OWNER_COMMAND_LINE];
        GetOwnerCommandRawValue(commandLine, password, sizeof(password));
        int length = strlen(password);
        if (length > 0 && length <= MAX_OWNER_JOIN_PASSWORD_LENGTH && StrContains(password, "\"") == -1)
            return true;
        Format(
            message, messageSize,
            "sv_password must be set to 1-%d characters without quotes; the server cannot be left open.",
            MAX_OWNER_JOIN_PASSWORD_LENGTH
        );
        return false;
    }

    if (StrEqual(operation, "log"))
    {
        // Logging feeds logs.tf and the log receivers, so it only goes on.
        if (valueArgumentCount == 0 || (valueArgumentCount == 1 && StrEqual(firstValue, "on", false)))
            return true;
        strcopy(message, messageSize, "Only \"log on\" is allowed.");
        return false;
    }

    if (StrEqual(operation, "sm_whitelist_id"))
    {
        if (valueArgumentCount == 0 || (valueArgumentCount == 1 && IsPlainIdentifier(firstValue, 64)))
            return true;
        strcopy(message, messageSize, "sm_whitelist_id takes one whitelist.tf ID of letters, numbers, and underscores.");
        return false;
    }

    bool isLogAdd = StrEqual(operation, "logaddress_add");
    if (isLogAdd || StrEqual(operation, "logaddress_del"))
    {
        // Log packets come from the server's own address, so a receiver would
        // learn the address that Steam's relays keep hidden.
        if (isLogAdd && !g_cvDirectConnect.BoolValue)
        {
            strcopy(message, messageSize, "Log receivers need a reservation with direct connections enabled.");
            return false;
        }

        char rawAddress[MAX_OWNER_COMMAND_LINE];
        char address[32];
        int octets[4];
        GetOwnerCommandRawValue(commandLine, rawAddress, sizeof(rawAddress));
        if (!ParseLogAddress(rawAddress, address, sizeof(address), octets))
        {
            Format(message, messageSize, "%s takes one IPv4 address and port, such as 203.0.113.5:27100.", operation);
            return false;
        }

        if (!isLogAdd)
            return true;

        if (!IsPublicIPv4(octets))
        {
            strcopy(message, messageSize, "Logs can only be sent to a public IPv4 address.");
            return false;
        }

        int unused;
        if (!g_mOwnerLogAddresses.GetValue(address, unused)
            && g_mOwnerLogAddresses.Size >= MAX_OWNER_LOG_ADDRESSES)
        {
            Format(
                message, messageSize,
                "At most %d log receivers can be added. Remove one with logaddress_del first.",
                MAX_OWNER_LOG_ADDRESSES
            );
            return false;
        }
        return true;
    }

    return true;
}

// Keeps g_mOwnerLogAddresses in step with the engine's list once a log
// receiver command has passed its checks and is about to run.
void TrackOwnerLogAddress(const char[] operation, const char[] commandLine)
{
    if (StrEqual(operation, "logaddress_delall"))
    {
        g_mOwnerLogAddresses.Clear();
        return;
    }

    bool isLogAdd = StrEqual(operation, "logaddress_add");
    if (!isLogAdd && !StrEqual(operation, "logaddress_del"))
        return;

    char rawAddress[MAX_OWNER_COMMAND_LINE];
    char address[32];
    int octets[4];
    GetOwnerCommandRawValue(commandLine, rawAddress, sizeof(rawAddress));
    if (!ParseLogAddress(rawAddress, address, sizeof(address), octets))
        return;

    if (isLogAdd)
        g_mOwnerLogAddresses.SetValue(address, 1);
    else
        g_mOwnerLogAddresses.Remove(address);
}

bool TryConsumeOwnerCommandCooldown()
{
    float now = GetEngineTime();
    float lastCommand = g_fLastOwnerCommand;

    if (lastCommand > 0.0 && now - lastCommand < OWNER_COMMAND_COOLDOWN)
        return false;

    g_fLastOwnerCommand = now;
    return true;
}

void LogOwnerCommand(
    const char[] actorSteamID,
    const char[] commandLine,
    bool viaRcon,
    const char[] outcome
)
{
    char safeCommand[MAX_OWNER_COMMAND_LINE];
    strcopy(safeCommand, sizeof(safeCommand), commandLine);

    for (int i = 0; safeCommand[i] != '\0'; i++)
    {
        int character = safeCommand[i];
        if ((character > 0 && character < 32) || character == 127)
            safeCommand[i] = ' ';
        else if (character == '"')
            safeCommand[i] = '\'';
    }

    // Keep passwords out of the log, including ones the checks refuse.
    for (int i = 0; i < sizeof(g_sOwnerSecretCommands); i++)
    {
        int length = strlen(g_sOwnerSecretCommands[i]);
        if (strncmp(safeCommand, g_sOwnerSecretCommands[i], length, false) == 0
            && safeCommand[length] == ' ')
        {
            Format(safeCommand, sizeof(safeCommand), "%s <hidden>", g_sOwnerSecretCommands[i]);
            break;
        }
    }

    LogMessage(
        "[summon] Owner command reservation=%d actor=%s command=\"%s\" outcome=%s%s",
        g_cvReservationNumber.IntValue,
        actorSteamID,
        safeCommand,
        outcome,
        viaRcon ? " via=rcon" : ""
    );
}

// ============================================================================
// Player Address Privacy
// ============================================================================

bool IsAsciiDigit(int character)
{
    return character >= '0' && character <= '9';
}

// Returns the length of the dotted IPv4 address that starts at text[start],
// or 0 when there is none. previous is the character before it. Longer dotted
// numbers, such as version strings, are not addresses.
int IPv4AddressLength(const char[] text, int start, int previous)
{
    if (IsAsciiDigit(previous) || previous == '.')
        return 0;

    int index = start;
    for (int part = 0; part < 4; part++)
    {
        int digits = 0;
        while (IsAsciiDigit(text[index]) && digits <= 3)
        {
            index++;
            digits++;
        }
        if (digits == 0 || digits > 3)
            return 0;
        if (part < 3)
        {
            if (text[index] != '.')
                return 0;
            index++;
        }
    }

    if (text[index] == '.' && IsAsciiDigit(text[index + 1]))
        return 0;
    return index - start;
}

// Replaces every IPv4 address in text with MASKED_IPV4 and keeps any port.
// Returns how many addresses were replaced. It works in place: status output
// can be 4 KB, and a second buffer that size does not fit on the plugin's
// heap beside the command's other buffers.
int MaskIPv4Addresses(char[] text)
{
    char masked[] = MASKED_IPV4;
    int written = 0;
    int count = 0;
    // The original character before text[index]; text[index - 1] may have
    // been overwritten already.
    int previous = '\0';

    for (int index = 0; text[index] != '\0'; )
    {
        int length = IsAsciiDigit(text[index]) ? IPv4AddressLength(text, index, previous) : 0;
        if (length > 0)
        {
            // MASKED_IPV4 is never longer than an address, so writing it
            // only replaces characters that have been read.
            for (int i = 0; masked[i] != '\0'; i++)
                text[written++] = masked[i];
            index += length;
            // An address ends with a digit.
            previous = '0';
            count++;
            continue;
        }
        previous = text[index];
        text[written++] = text[index++];
    }

    text[written] = '\0';
    return count;
}

// Appends one line to the private player address log, in the game log's
// format. LogToFileEx would also echo the line to the server console.
void WritePlayerAddressLog(const char[] line)
{
    File file = OpenFile(g_sPlayerAddressLogPath, "a");
    if (file == null)
    {
        LogError("[summon] Could not open %s", g_sPlayerAddressLogPath);
        return;
    }

    char time[32];
    FormatTime(time, sizeof(time), "%m/%d/%Y - %H:%M:%S");
    file.WriteLine("L %s: %s", time, line);
    delete file;
}

// The game logs each player's address once, when they connect:
//   "Name<2><[U:1:123]><>" connected, address "203.0.113.9:27005"
// The game log goes to the log file and to every log receiver, and owners
// can add receivers. So Summon writes the line as the game sent it to a
// SourceMod log that only the operator's archive collects, and logs a copy
// with the address masked in its place. SourceMod hands that copy straight to
// the engine, so it does not pass through this hook again. Bots and SourceTV
// log "none" as their address and are left alone.
public Action GameLog_MaskPlayerAddresses(const char[] message)
{
    // Most lines are not connect lines, so they leave here.
    if (StrContains(message, "connected, address \"") == -1)
        return Plugin_Continue;

    // The game ends each line with a line break, and LogToGame adds one.
    char line[MAX_GAME_LOG_LINE];
    strcopy(line, sizeof(line), message);
    int length = strlen(line);
    while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r'))
        line[--length] = '\0';

    // The address is the last quoted value on the line. Reading it from the
    // end means a player name that contains the same words cannot move it.
    if (length < 2 || line[length - 1] != '"')
        return Plugin_Continue;
    int valueStart = length - 1;
    while (valueStart > 0 && line[valueStart - 1] != '"')
        valueStart--;
    int markerLength = strlen("connected, address \"");
    int markerStart = valueStart - markerLength;
    if (markerStart < 0
        || strncmp(line[markerStart], "connected, address \"", markerLength) != 0)
    {
        return Plugin_Continue;
    }

    char address[MAX_GAME_LOG_LINE];
    strcopy(address, length - valueStart, line[valueStart]);
    if (MaskIPv4Addresses(address) == 0)
        return Plugin_Continue;

    WritePlayerAddressLog(line);
    line[valueStart] = '\0';
    LogToGame("%s%s\"", line, address);
    return Plugin_Handled;
}

// ============================================================================
// Restricted Owner Server Commands
// ============================================================================

public Action Command_OwnerServerCommand(int client, int args)
{
    if (client == 0)
    {
        PrintToServer("[summon] sm_command and sm_cmd are available to the reservation owner in-game");
        return Plugin_Stop;
    }

    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Stop;

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can run server commands.");
        return Plugin_Stop;
    }

    char invokedCommand[16];
    GetCmdArg(0, invokedCommand, sizeof(invokedCommand));
    if (StrEqual(invokedCommand, "sm_cmd", false))
    {
        HandleOwnerServerCommand(client, args, "cmd");
    }
    else
    {
        HandleOwnerServerCommand(client, args, "command");
    }
    return Plugin_Stop;
}

public Action Listener_OwnerRcon(int client, const char[] command, int args)
{
    // Server console and the agent's authenticated container-local RCON path
    // must retain the normal unrestricted behavior.
    if (client == 0)
        return Plugin_Continue;

    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Stop;

    // Preserve stock sm_rcon for callers who already have its configured
    // SourceMod access. This branch intentionally comes before IsOwner so a
    // site admin who owns a reservation keeps their existing privileges.
    if (CheckCommandAccess(client, command, ADMFLAG_RCON, false))
        return Plugin_Continue;

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can run server commands.");
        return Plugin_Stop;
    }

    // Every owner outcome is handled here so the raw argument string can
    // never fall through to SourceMod's unrestricted sm_rcon callback.
    HandleOwnerServerCommand(client, args, "rcon");
    return Plugin_Stop;
}

public Action Listener_OwnerAdmin(int client, const char[] command, int args)
{
    if (client == 0)
        return Plugin_Continue;

    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Stop;

    // SourceMod administrators keep the stock admin menu. Command overrides
    // are honored because override_only is false.
    if (CheckCommandAccess(client, command, ADMFLAG_GENERIC, false))
        return Plugin_Continue;

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can open the Summon menu.");
        return Plugin_Stop;
    }

    ShowOwnerAdminMenu(client);
    return Plugin_Stop;
}

public Action Listener_ReservationWho(int client, const char[] command, int args)
{
    if (client == 0)
        return Plugin_Continue;

    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Stop;

    // Keep SourceMod's admin-identification command for callers who can use
    // it; everyone else gets Summon's public reservation information.
    if (CheckCommandAccess(client, command, ADMFLAG_GENERIC, false))
        return Plugin_Continue;

    Command_Reservation(client, args);
    return Plugin_Stop;
}

public Action Listener_OwnerMap(int client, const char[] command, int args)
{
    if (client == 0)
        return Plugin_Continue;

    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Stop;

    // Preserve stock sm_map for administrators with map-change access. An
    // owner without that access is routed through Summon's permission gate
    // and map-downloader forward instead.
    if (CheckCommandAccess(client, command, ADMFLAG_CHANGEMAP, false))
        return Plugin_Continue;

    HandleOwnerMapCommand(client, args, "map");
    return Plugin_Stop;
}

Action HandleOwnerServerCommand(int client, int args, const char[] trigger)
{
    if (args < 1)
    {
        PrintToChat(
            client,
            "\x01[\x07FF6600Summon\x01] Usage: \x0799FF99!%s <command> [arguments...]\x01. Available commands are configured by the server.",
            trigger
        );
        return Plugin_Handled;
    }

    char operation[256];
    int operationLength = GetCmdArg(1, operation, sizeof(operation));
    char commandLine[MAX_OWNER_COMMAND_LINE + 1];
    int commandLength = GetCmdArgString(commandLine, sizeof(commandLine));
    char actorSteamID[32];
    if (!GetClientAuthId(client, AuthId_SteamID64, actorSteamID, sizeof(actorSteamID)))
        strcopy(actorSteamID, sizeof(actorSteamID), "unknown");

    char firstValue[PLATFORM_MAX_PATH];
    if (args >= 2)
        GetCmdArg(2, firstValue, sizeof(firstValue));

    // Running the command can change the reply source, so keep the caller's.
    ReplySource replySource = GetCmdReplySource();
    char response[MAX_OWNER_COMMAND_RESPONSE];
    char errorCode[64];
    char errorMessage[256];
    bool succeeded = ExecuteOwnerServerCommand(
        actorSteamID,
        false,
        operation,
        operationLength,
        commandLine,
        commandLength,
        args - 1,
        firstValue,
        response,
        sizeof(response),
        errorCode,
        sizeof(errorCode),
        errorMessage,
        sizeof(errorMessage)
    );

    if (!succeeded)
    {
        if (IsClientConnected(client))
            PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666%s", errorMessage);
        return Plugin_Handled;
    }

    // A loaded config is already announced to everyone, including the owner.
    if (!IsClientConnected(client) || StrEqual(operation, OWNER_CONFIG_COMMAND))
        return Plugin_Handled;

    if (IsOwnerMapListing(operation, args - 1))
    {
        ReplyOwnerMapList(client, firstValue);
        if (replySource == SM_REPLY_TO_CHAT && IsClientInGame(client))
            PrintToChat(client, "\x01[\x07FF6600Summon\x01] The output is in your console.");
        return Plugin_Handled;
    }

    if (response[0] != '\0')
    {
        ReplyOwnerCommandOutput(client, replySource, response);
    }
    else if (IsClientInGame(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] Command \x0799FF99%s\x01 dispatched.", operation);
    }

    return Plugin_Handled;
}

// A chat message holds one short line, so longer output, such as the player
// list from status, goes to the owner's console line by line instead.
void ReplyOwnerCommandOutput(int client, ReplySource replySource, const char[] response)
{
    bool multiline = StrContains(response, "\n") != -1;
    if (!multiline
        && (replySource != SM_REPLY_TO_CHAT || strlen(response) <= MAX_CHAT_REPLY_LENGTH))
    {
        ReplySource previous = SetCmdReplySource(replySource);
        ReplyToCommand(client, "%s", response);
        SetCmdReplySource(previous);
        return;
    }

    char line[MAX_GAME_LOG_LINE];
    int start = 0;
    while (response[start] != '\0')
    {
        int end = start;
        while (response[end] != '\0' && response[end] != '\n')
            end++;
        int length = end - start;
        if (length >= sizeof(line))
            length = sizeof(line) - 1;
        strcopy(line, length + 1, response[start]);
        PrintToConsole(client, "%s", line);
        start = response[end] == '\n' ? end + 1 : end;
    }

    if (replySource == SM_REPLY_TO_CHAT && IsClientInGame(client))
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] The output is in your console.");
}

bool OwnerCommandFailure(
    const char[] actorSteamID,
    const char[] commandLine,
    bool viaRcon,
    const char[] code,
    const char[] message,
    char[] errorCode,
    int errorCodeSize,
    char[] errorMessage,
    int errorMessageSize
)
{
    strcopy(errorCode, errorCodeSize, code);
    strcopy(errorMessage, errorMessageSize, message);
    LogOwnerCommand(actorSteamID, commandLine, viaRcon, code);
    return false;
}

// viaRcon marks a command the agent's RCON gateway forwarded. Every route
// shares one allowlist and one set of argument rules; RCON only skips the
// cooldown, which the gateway replaces with its own rate limit.
bool ExecuteOwnerServerCommand(
    const char[] actorSteamID,
    bool viaRcon,
    char[] operation,
    int operationLength,
    const char[] commandLine,
    int commandLength,
    int valueArgumentCount,
    const char[] firstValue,
    char[] response,
    int responseSize,
    char[] errorCode,
    int errorCodeSize,
    char[] errorMessage,
    int errorMessageSize
)
{
    response[0] = '\0';
    errorCode[0] = '\0';
    errorMessage[0] = '\0';

    if (!ReservationAllowsOwnerServerCommands())
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "reservation_inactive",
            "The reservation is not active.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (operationLength <= 0
        || operationLength >= 255
        || !NormalizeOwnerCommandName(operation))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "invalid_command_name",
            "That server command name is invalid.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (g_mOwnerCommandAllowlist == null || g_iOwnerCommandCount <= 0)
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "allowlist_unavailable",
            "The server command allowlist is unavailable.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    bool isConfigCommand = StrEqual(operation, OWNER_CONFIG_COMMAND);
    if (!isConfigCommand
        && !g_mOwnerCommandAllowlist.ContainsKey(operation))
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "command_not_allowed",
            "That server command is not allowed.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (!CommandExists(operation)
        || (StrEqual(operation, OWNER_CHEATS_COMMAND) && !IsCheatsGuardAvailable()))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "command_unavailable",
            "That server command is unavailable.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (commandLength <= 0
        || commandLength >= MAX_OWNER_COMMAND_LINE
        || !IsSafeOwnerCommandLine(commandLine))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "unsafe_command",
            "That command line is not safe to run.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    char argumentError[256];
    if (!AreOwnerCommandArgumentsAllowed(
        operation, valueArgumentCount, firstValue, commandLine,
        argumentError, sizeof(argumentError)
    ))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon,
            isConfigCommand ? "unknown_config" : "invalid_arguments",
            argumentError,
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (!viaRcon && !TryConsumeOwnerCommandCooldown())
        return OwnerCommandFailure(
            actorSteamID, commandLine, viaRcon, "cooldown",
            "Please wait before running another server command.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (isConfigCommand)
    {
        char configName[64];
        GetOwnerConfigName(firstValue, configName, sizeof(configName));
        ExecuteConfig(configName);
        PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Loaded config: \x07FFFF00%s", configName);
        Format(response, responseSize, "Loaded config: %s", configName);
        LogOwnerCommand(actorSteamID, commandLine, viaRcon, "ok");
        return true;
    }

    // The caller prints the list, because it can be longer than response.
    if (IsOwnerMapListing(operation, valueArgumentCount))
    {
        LogOwnerCommand(actorSteamID, commandLine, viaRcon, "ok");
        return true;
    }

    TrackOwnerLogAddress(operation, commandLine);

    // Match stock sm_rcon's synchronous execution/output behavior. The fixed
    // format string prevents user input from becoming format directives.
    ServerCommandEx(response, responseSize, "%s", commandLine);
    TrimString(response);

    // status lists every player's address, and the server's own, which is
    // not shown to owners whose players connect through Steam's relays. Run
    // over RCON, a command prints its output after this returns, so the
    // agent's RCON gateway and the web console mask that output themselves.
    if (StrEqual(operation, "status"))
        MaskIPv4Addresses(response);

    // The engine prints nothing when sv_cheats is set. The output is other
    // plugins' log lines and StAC's error report, so state the result plainly.
    // Run from RCON, the command has not executed yet, so go by the requested
    // value, which the engine reads as a number.
    if (valueArgumentCount > 0 && StrEqual(operation, OWNER_CHEATS_COMMAND))
    {
        strcopy(
            response,
            responseSize,
            RoundToZero(StringToFloat(firstValue)) != 0
                ? "Cheats are on. Maps cannot run server commands until the map changes with cheats off."
                : "Cheats are off. Change the map to remove anything players set up while they were on."
        );
    }

    LogOwnerCommand(actorSteamID, commandLine, viaRcon, "ok");
    return true;
}

bool IsValidOwnerCommandActor(const char[] actorSteamID)
{
    if (strlen(actorSteamID) != 17)
        return false;
    for (int i = 0; actorSteamID[i] != '\0'; i++)
    {
        if (actorSteamID[i] < '0' || actorSteamID[i] > '9')
            return false;
    }
    return true;
}

bool ExtractServerOwnerCommandLine(char[] commandLine, int commandLineSize)
{
    char allArguments[MAX_OWNER_COMMAND_LINE + 64];
    int length = GetCmdArgString(allArguments, sizeof(allArguments));
    if (length <= 0 || length >= sizeof(allArguments) - 1)
        return false;

    int index = 0;
    while (allArguments[index] == ' ')
        index++;
    while (allArguments[index] != '\0' && allArguments[index] != ' ')
        index++;
    while (allArguments[index] == ' ')
        index++;

    int commandLength = strlen(allArguments[index]);
    if (commandLength <= 0 || commandLength >= commandLineSize)
        return false;
    strcopy(commandLine, commandLineSize, allArguments[index]);
    TrimString(commandLine);
    return commandLine[0] != '\0';
}

public Action Command_ServerOwnerCommand(int args)
{
    return RunServerOwnerCommand(args, false);
}

// Called by the agent's RCON gateway with one command a remote RCON client
// sent. The gateway forwards one command per call and never a semicolon or
// line break, which the engine would split before this callback ran.
public Action Command_ServerRconCommand(int args)
{
    return RunServerOwnerCommand(args, true);
}

Action RunServerOwnerCommand(int args, bool viaRcon)
{
    char actorSteamID[32];
    if (args >= 1)
        GetCmdArg(1, actorSteamID, sizeof(actorSteamID));

    char commandLine[MAX_OWNER_COMMAND_LINE];
    if (args < 2
        || !IsValidOwnerCommandActor(actorSteamID)
        || !ExtractServerOwnerCommandLine(commandLine, sizeof(commandLine)))
    {
        PrintToServer("%s ERROR invalid_request", OWNER_COMMAND_RESULT_MARKER);
        PrintToServer("The owner command request is invalid.");
        LogOwnerCommand(actorSteamID, "<invalid>", viaRcon, "invalid_request");
        return Plugin_Handled;
    }

    char operation[256];
    int operationLength = GetCmdArg(2, operation, sizeof(operation));
    char firstValue[PLATFORM_MAX_PATH];
    if (args >= 3)
        GetCmdArg(3, firstValue, sizeof(firstValue));

    char response[MAX_OWNER_COMMAND_RESPONSE];
    char errorCode[64];
    char errorMessage[256];
    bool succeeded = ExecuteOwnerServerCommand(
        actorSteamID,
        viaRcon,
        operation,
        operationLength,
        commandLine,
        strlen(commandLine),
        args - 2,
        firstValue,
        response,
        sizeof(response),
        errorCode,
        sizeof(errorCode),
        errorMessage,
        sizeof(errorMessage)
    );

    if (!succeeded)
    {
        PrintToServer("%s ERROR %s", OWNER_COMMAND_RESULT_MARKER, errorCode);
        PrintToServer("%s", errorMessage);
        return Plugin_Handled;
    }

    PrintToServer("%s OK", OWNER_COMMAND_RESULT_MARKER);
    if (IsOwnerMapListing(operation, args - 2))
        ReplyOwnerMapList(0, firstValue);
    else if (response[0] != '\0')
        PrintToServer("%s", response);
    return Plugin_Handled;
}

// ============================================================================
// Expiry Countdown
// ============================================================================

// Thresholds (in seconds remaining) at which to show warnings.
// Hours/minutes use yellow, final minute uses red, seconds use red.
public Action Timer_ExpiryCheck(Handle timer)
{
    int endsAt = g_cvEndsAt.IntValue;
    if (endsAt == 0)
        return Plugin_Continue;

    int remaining = endsAt - GetTime();

    // Show warnings at specific thresholds
    switch (remaining)
    {
        case 10800: PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 3 hours.");
        case 7200:  PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 2 hours.");
        case 3600:  PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 1 hour.");
        case 1800:  PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 30 minutes.");
        case 900:   PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 15 minutes.");
        case 300:   PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in 5 minutes.");
        case 60:    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 1 minute!");
        case 30:    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 30 seconds!");
        case 20:    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 20 seconds!");
        case 10:    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 10 seconds!");
        case 5:     PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 5 seconds!");
        case 4:     PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 4 seconds!");
        case 3:     PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 3 seconds!");
        case 2:     PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 2 seconds!");
        case 1:     PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 1 second!");
    }

    // Kick all players when time expires
    if (remaining <= 0 && !g_bExpiryKickDone)
    {
        g_bExpiryKickDone = true;
        PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Reservation has expired. Thanks for playing!");

        for (int i = 1; i <= MaxClients; i++)
        {
            if (IsClientInGame(i) && !IsFakeClient(i))
            {
                KickClient(i, "Reservation expired. Thanks for playing!");
            }
        }

        // Stop the timer
        g_hExpiryTimer = INVALID_HANDLE;
        return Plugin_Stop;
    }

    return Plugin_Continue;
}

// ============================================================================
// Chat Commands
// ============================================================================

public Action Command_Pause(int client, int args)
{
    return HandlePauseCommand(client, args, "pause", "setpause");
}

public Action Command_Unpause(int client, int args)
{
    return HandlePauseCommand(client, args, "unpause", "unpause");
}

Action HandlePauseCommand(int client, int args, const char[] trigger, const char[] command)
{
    if (client == 0)
    {
        ReplyToCommand(client, "[SM] Pause commands must be used by a player in game.");
        return Plugin_Handled;
    }
    if (client < 1 || client > MaxClients || !IsClientInGame(client) || IsFakeClient(client))
        return Plugin_Handled;

    if (args != 0)
    {
        ReplyToCommand(client, "[SM] Usage: !%s", trigger);
        return Plugin_Handled;
    }

    ConVar pausable = FindConVar("sv_pausable");
    if (pausable == null || !pausable.BoolValue)
    {
        ReplyToCommand(client, "[SM] Pausing is disabled by the current config.");
        return Plugin_Handled;
    }

    // Keep F2's player attribution, state restoration, and unpause countdown.
    // setpause is explicit, so repeated !pause requests cannot toggle an unpause.
    FakeClientCommandEx(client, "%s", command);
    return Plugin_Handled;
}

public Action Command_Reservation(int client, int args)
{
    char ownerName[64];
    char timeRemaining[64];

    g_cvOwnerName.GetString(ownerName, sizeof(ownerName));
    int reservationNumber = g_cvReservationNumber.IntValue;
    int remaining = GetTimeRemaining();

    FormatTimeRemaining(remaining, timeRemaining, sizeof(timeRemaining));

    PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x0799FF99Reservation #%d", reservationNumber);
    PrintToChat(client, "\x01[\x07FF6600Summon\x01] Reserved by: \x0799FF99%s", ownerName);
    PrintToChat(client, "\x01[\x07FF6600Summon\x01] Time remaining: \x0799FF99%s", timeRemaining);

    return Plugin_Handled;
}

void ShowOwnerAdminMenu(int client)
{
    Menu menu = new Menu(OwnerAdminMenuHandler);

    char title[64];
    Format(title, sizeof(title), "Summon - Reservation #%d", g_cvReservationNumber.IntValue);
    menu.SetTitle(title);
    menu.AddItem("reservation", "Reservation Information");
    menu.AddItem("map", "Change Map");
    menu.AddItem("config", "Load Competitive Config");
    menu.AddItem("restart", "Restart Match");

    if (g_bEndCountdownActive)
        menu.AddItem("cancel", "Cancel Reservation End");
    else
        menu.AddItem("end", "End Reservation");

    menu.Display(client, 30);
}

public int OwnerAdminMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        // Menu callbacks can run after the active reservation changes. Check
        // ownership again before exposing or executing an owner function.
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can use this menu.");
            return 0;
        }

        char info[32];
        menu.GetItem(param2, info, sizeof(info));

        if (StrEqual(info, "reservation"))
            Command_Reservation(param1, 0);
        else if (StrEqual(info, "map"))
            ShowOwnerMapMenu(param1);
        else if (StrEqual(info, "config"))
            ShowLeagueMenu(param1);
        else if (StrEqual(info, "restart"))
            ShowRestartMenu(param1);
        else if (StrEqual(info, "end"))
            Command_End(param1, 0);
        else if (StrEqual(info, "cancel"))
            Command_Cancel(param1, 0);
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
}

public Action Command_End(int client, int args)
{
    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the reservation owner can end the reservation.");
        return Plugin_Handled;
    }

    if (g_bEndCountdownActive)
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] End countdown already in progress. Use \x0799FF99!cancel\x01 to abort.");
        return Plugin_Handled;
    }

    // Start the end countdown
    g_bEndCountdownActive = true;
    g_iEndCountdown = END_COUNTDOWN_SECONDS;

    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00Reservation ending in %d seconds!", END_COUNTDOWN_SECONDS);
    PrintToChatAll("\x01[\x07FF6600Summon\x01] Type \x0799FF99!cancel\x01 to abort.");

    g_hEndTimer = CreateTimer(1.0, Timer_EndCountdown, _, TIMER_REPEAT);

    return Plugin_Handled;
}

public Action Timer_EndCountdown(Handle timer)
{
    g_iEndCountdown--;

    if (!g_bEndCountdownActive)
    {
        // Countdown was cancelled
        g_hEndTimer = INVALID_HANDLE;
        return Plugin_Stop;
    }

    if (g_iEndCountdown <= 0)
    {
        // Countdown finished - end the reservation
        g_bEndCountdownActive = false;
        g_hEndTimer = INVALID_HANDLE;

        ExecuteReservationEnd();
        return Plugin_Stop;
    }

    // Show countdown at 5, 4, 3, 2, 1
    if (g_iEndCountdown <= 5)
    {
        PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00Reservation ending in %d seconds...", g_iEndCountdown);
    }

    return Plugin_Continue;
}

void ExecuteReservationEnd()
{
    // Call backend to end reservation
    char backendURL[256];
    char apiKey[64];

    g_cvBackendURL.GetString(backendURL, sizeof(backendURL));
    g_cvAPIKey.GetString(apiKey, sizeof(apiKey));

    int reservationNumber = g_cvReservationNumber.IntValue;

    if (strlen(backendURL) > 0 && strlen(apiKey) > 0)
    {
        char url[512];
        Format(url, sizeof(url), "%s/internal/reservations/%d/end", backendURL, reservationNumber);

        HTTPRequest request = new HTTPRequest(url);
        request.SetHeader("X-API-Key", apiKey);
        request.SetHeader("Content-Type", "application/json");

        // Create empty JSON body for POST
        JSONObject body = new JSONObject();
        request.Post(body, OnEndResponse);
        delete body;
    }

    // Show final message
    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Thanks for playing!");

    // Kick all players after 5 seconds
    CreateTimer(5.0, Timer_KickAll);
}

public void OnEndResponse(HTTPResponse response, any data, const char[] error)
{
    if (strlen(error) > 0)
    {
        LogError("[summon] Failed to notify backend of reservation end: %s", error);
        return;
    }

    if (response.Status != HTTPStatus_OK)
    {
        LogError("[summon] Backend returned HTTP %d for reservation end", response.Status);
    }
}

public Action Timer_KickAll(Handle timer)
{
    for (int i = 1; i <= MaxClients; i++)
    {
        if (IsClientInGame(i) && !IsFakeClient(i))
        {
            KickClient(i, "Reservation ended. Thanks for playing!");
        }
    }
    return Plugin_Stop;
}

public Action Command_Cancel(int client, int args)
{
    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the reservation owner can cancel.");
        return Plugin_Handled;
    }

    if (!g_bEndCountdownActive)
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] No end countdown active.");
        return Plugin_Handled;
    }

    g_bEndCountdownActive = false;

    if (g_hEndTimer != INVALID_HANDLE)
    {
        KillTimer(g_hEndTimer);
        g_hEndTimer = INVALID_HANDLE;
    }

    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Reservation end cancelled.");

    return Plugin_Handled;
}

// ============================================================================
// Map Change (permission gate -- download logic is in mapdownloader plugin)
// ============================================================================

public Action Command_Map(int client, int args)
{
    if (client == 0)
    {
        // Server console: let the mapdownloader plugin handle it directly
        return Plugin_Continue;
    }

    return HandleOwnerMapCommand(client, args, "changemap");
}

Action HandleOwnerMapCommand(int client, int args, const char[] trigger)
{
    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the reservation owner can change the map.");
        return Plugin_Handled;
    }

    if (args < 1)
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] Usage: !%s <mapname>", trigger);
        return Plugin_Handled;
    }

    char mapName[PLATFORM_MAX_PATH];
    GetCmdArg(1, mapName, sizeof(mapName));

    RequestOwnerMapChange(client, mapName);
    return Plugin_Handled;
}

void RequestOwnerMapChange(int client, const char[] mapName)
{
    // Fire forward so mapdownloader (or other plugins) can act on it.
    Action result;
    Call_StartForward(g_fwdOnMapChangeRequested);
    Call_PushCell(client);
    Call_PushString(mapName);
    Call_Finish(result);
}

void ShowOwnerMapMenu(int client)
{
    ArrayList names = CollectOwnerMapNames("*");
    if (names.Length == 0)
    {
        delete names;
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666No maps to display. Use \x0799FF99!map <mapname>\x01 instead.");
        return;
    }

    Menu menu = new Menu(OwnerMapMenuHandler);
    menu.SetTitle("Change Map");
    menu.ExitBackButton = true;

    char mapName[MAX_MAP_NAME_LENGTH];
    for (int i = 0; i < names.Length; i++)
    {
        names.GetString(i, mapName, sizeof(mapName));
        menu.AddItem(mapName, mapName);
    }

    delete names;
    menu.Display(client, 30);
}

public int OwnerMapMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can change the map.");
            return 0;
        }

        char mapName[PLATFORM_MAX_PATH];
        menu.GetItem(param2, mapName, sizeof(mapName));
        RequestOwnerMapChange(param1, mapName);
    }
    else if (action == MenuAction_Cancel && param2 == MenuCancel_ExitBack)
    {
        if (IsOwner(param1))
            ShowOwnerAdminMenu(param1);
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
}

// ============================================================================
// SourceTV Wait
// ============================================================================

// SourceTV viewers see the game tv_delay seconds late, and a map change cuts
// off what they have not seen yet. During or after a tournament match, a map
// change waits for them to catch up, but only while someone watches SourceTV
// or a relay is connected. Demos are recorded in real time and lose nothing.
// A second changelevel during the wait changes the map at once.

bool IsRGLQoLLoaded()
{
    return FindPluginByFile("rglqol.smx") != null;
}

bool IsTournamentMatchLive()
{
    if (g_cvTournament == null || !g_cvTournament.BoolValue)
        return false;

    // Teams have not readied up yet, or the match ended and the server went
    // back to waiting for them.
    if (GameRules_GetProp("m_bAwaitingReadyRestart") || GameRules_GetProp("m_bInWaitingForPlayers"))
        return false;

    RoundState state = GameRules_GetRoundState();
    return state != RoundState_Pregame && state != RoundState_GameOver;
}

public Action Timer_MatchState(Handle timer)
{
    if (IsTournamentMatchLive())
        g_fMatchLastLiveTime = GetEngineTime();
    else
        CheckAutoHighlander();
    return Plugin_Continue;
}

// Returns how many seconds a map change must wait for SourceTV viewers to
// catch up with this map's match, or 0 if it need not wait.
int GetSTVWaitSeconds()
{
    // RGL's QoL plugin holds map changes after a match itself.
    if (IsRGLQoLLoaded())
        return 0;

    ConVar tvEnable = FindConVar("tv_enable");
    ConVar tvDelay = FindConVar("tv_delay");
    if (tvEnable == null || tvDelay == null || !tvEnable.BoolValue || tvDelay.FloatValue <= 0.0)
        return 0;

    if (IsTournamentMatchLive())
        return RoundToCeil(tvDelay.FloatValue);

    if (g_fMatchLastLiveTime <= 0.0)
        return 0;

    float remaining = g_fMatchLastLiveTime + tvDelay.FloatValue + STV_WAIT_MARGIN - GetEngineTime();
    if (remaining < 1.0)
        return 0;
    return RoundToCeil(remaining);
}

// Reads the spectators and relays connected to this server from tv_status.
// Viewers on a relay are not counted, but their relay is. Returns false if
// the output cannot be read.
bool GetSTVAudience(int &spectators, int &relays)
{
    spectators = 0;
    relays = 0;

    char status[2048];
    ServerCommandEx(status, sizeof(status), "tv_status");

    if (StrContains(status, "SourceTV not active") != -1)
        return true;

    int local = StrContains(status, "Local Slots ");
    if (local == -1)
        return false;

    int spectatorsOffset = StrContains(status[local], "Spectators ");
    if (spectatorsOffset == -1)
        return false;
    spectatorsOffset += local + strlen("Spectators ");

    int relaysOffset = StrContains(status[spectatorsOffset], "Proxies ");
    if (relaysOffset == -1)
        return false;
    relaysOffset += spectatorsOffset + strlen("Proxies ");

    return StringToIntEx(status[spectatorsOffset], spectators) > 0
        && StringToIntEx(status[relaysOffset], relays) > 0;
}

public Action Command_Changelevel(int args)
{
    // g_bSTVWaitActive stays set until the map changes, so a second
    // changelevel, and Summon's own when the wait ends, go straight through.
    if (args < 1 || g_bSTVWaitActive)
        return Plugin_Continue;

    int waitSeconds = GetSTVWaitSeconds();
    if (waitSeconds <= 0)
        return Plugin_Continue;

    // mapdownloader handles a missing map and changes the level again once
    // it is downloaded.
    char mapName[PLATFORM_MAX_PATH];
    GetCmdArg(1, mapName, sizeof(mapName));
    if (!IsMapValid(mapName))
        return Plugin_Continue;

    g_bSTVWaitActive = true;
    g_iSTVWaitStart = waitSeconds;
    g_iSTVWaitRemaining = waitSeconds;
    strcopy(g_sSTVWaitMap, sizeof(g_sSTVWaitMap), mapName);

    // Inside a command run from the command buffer, as changelevel always
    // is, ServerCommandEx returns before tv_status has printed anything.
    RequestFrame(Frame_StartSTVWait);
    return Plugin_Stop;
}

public void Frame_StartSTVWait()
{
    // Cancelled, or the map changed some other way.
    if (!g_bSTVWaitActive)
        return;

    int spectators, relays;
    if (!GetSTVAudience(spectators, relays))
    {
        LogError(
            "[summon] Could not read tv_status; changelevel to %s waits %d seconds for SourceTV",
            g_sSTVWaitMap,
            g_iSTVWaitRemaining
        );
    }
    else if (spectators == 0 && relays == 0)
    {
        LogMessage("[summon] Nobody is watching SourceTV; changelevel to %s does not wait", g_sSTVWaitMap);
        ForceChangeLevel(g_sSTVWaitMap, "Summon: nobody is watching SourceTV");
        return;
    }
    else
    {
        LogMessage(
            "[summon] Changelevel to %s waits %d seconds for %d SourceTV spectator(s) and %d relay(s)",
            g_sSTVWaitMap,
            g_iSTVWaitRemaining,
            spectators,
            relays
        );
    }

    PrintToChatAll(
        "\x01[\x07FF6600Summon\x01] Changing map to \x0799FF99%s\x01 in %d seconds so SourceTV viewers can catch up.",
        g_sSTVWaitMap,
        g_iSTVWaitRemaining
    );
    PrintToChatAll("\x01[\x07FF6600Summon\x01] Change the map again to skip the wait.");
    PrintToServer("[summon] To cancel the map change, run stopchangelevel.");
    g_hSTVWaitTimer = CreateTimer(1.0, Timer_STVWait, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
}

public Action Timer_STVWait(Handle timer)
{
    g_iSTVWaitRemaining--;

    if (g_iSTVWaitRemaining <= 0)
    {
        g_hSTVWaitTimer = INVALID_HANDLE;
        ForceChangeLevel(g_sSTVWaitMap, "Summon: SourceTV viewers caught up");
        return Plugin_Stop;
    }

    // tv_status also prints its output to the server console, so check only
    // every few seconds.
    int spectators, relays;
    if ((g_iSTVWaitStart - g_iSTVWaitRemaining) % 5 == 0
        && GetSTVAudience(spectators, relays)
        && spectators == 0
        && relays == 0)
    {
        g_hSTVWaitTimer = INVALID_HANDLE;
        LogMessage("[summon] SourceTV viewers left; changing level to %s", g_sSTVWaitMap);
        PrintToChatAll("\x01[\x07FF6600Summon\x01] Nobody is watching SourceTV any more. Changing map now.");
        ForceChangeLevel(g_sSTVWaitMap, "Summon: SourceTV viewers left");
        return Plugin_Stop;
    }

    // Skip reminders right after the first announcement.
    bool reminder = g_iSTVWaitRemaining == 60
        || g_iSTVWaitRemaining == 30
        || g_iSTVWaitRemaining == 10;
    if (reminder && g_iSTVWaitStart - g_iSTVWaitRemaining >= 5)
    {
        PrintToChatAll(
            "\x01[\x07FF6600Summon\x01] Changing map to \x0799FF99%s\x01 in %d seconds...",
            g_sSTVWaitMap,
            g_iSTVWaitRemaining
        );
    }

    if (g_iSTVWaitRemaining <= 5)
    {
        PrintHintTextToAll(
            "Changing map in %d second%s...",
            g_iSTVWaitRemaining,
            g_iSTVWaitRemaining == 1 ? "" : "s"
        );
    }

    return Plugin_Continue;
}

public Action Command_StopChangelevel(int args)
{
    if (!g_bSTVWaitActive)
    {
        PrintToServer("[summon] No map change is waiting for SourceTV.");
        return Plugin_Handled;
    }

    if (g_hSTVWaitTimer != INVALID_HANDLE)
    {
        KillTimer(g_hSTVWaitTimer);
        g_hSTVWaitTimer = INVALID_HANDLE;
    }
    g_bSTVWaitActive = false;

    LogMessage("[summon] Changelevel to %s cancelled", g_sSTVWaitMap);
    PrintToServer("[summon] Map change cancelled.");
    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Map change cancelled.");
    return Plugin_Handled;
}

void DisableTVDelayMapChangeProtect()
{
    if (g_cvTVDelayMapChangeProtect != null && g_cvTVDelayMapChangeProtect.BoolValue)
        g_cvTVDelayMapChangeProtect.BoolValue = false;
}

public void ConVar_TVDelayMapChangeProtect(ConVar convar, const char[] oldValue, const char[] newValue)
{
    // RGL configs keep the protection along with RGL's own wait.
    if (!IsRGLQoLLoaded())
        DisableTVDelayMapChangeProtect();
}

// ============================================================================
// Cheats Safeguard
// ============================================================================

// Players can leave entities and scripts behind while cheats are on, and
// those keep running after cheats are turned off. The safeguard therefore
// lasts until the first map load with cheats off, which removes them.

bool IsCheatsGuardAvailable()
{
    return g_cvCheats != null && g_cvPointServerCommand != null;
}

void StartCheatsGuard()
{
    if (!g_bCheatsGuardActive)
    {
        g_cvPointServerCommand.GetString(g_sPointServerCommandRestore, sizeof(g_sPointServerCommandRestore));
        g_bCheatsGuardActive = true;
        LogMessage("[summon] sv_cheats is on; map entities and scripts cannot run server commands until a map loads with it off");
    }

    g_cvPointServerCommand.SetString(POINT_SERVERCOMMAND_DISALLOW);
}

void StopCheatsGuard()
{
    g_bCheatsGuardActive = false;
    g_cvPointServerCommand.SetString(g_sPointServerCommandRestore);
    LogMessage("[summon] sv_allow_point_servercommand restored to %s", g_sPointServerCommandRestore);
}

public void ConVar_Cheats(ConVar convar, const char[] oldValue, const char[] newValue)
{
    if (convar.BoolValue)
    {
        StartCheatsGuard();
        return;
    }

    // StAC stops itself when cheats turn on and does not come back by itself.
    Handle stac = FindPluginByFile("stac.smx");
    if (stac != null && GetPluginStatus(stac) != Plugin_Running)
        ServerCommand("sm plugins reload stac");
}

public void ConVar_PointServerCommand(ConVar convar, const char[] oldValue, const char[] newValue)
{
    if (!g_bCheatsGuardActive || StrEqual(newValue, POINT_SERVERCOMMAND_DISALLOW, false))
        return;

    // A config or an admin asked for another value. It applies once the
    // safeguard ends.
    strcopy(g_sPointServerCommandRestore, sizeof(g_sPointServerCommandRestore), newValue);
    convar.SetString(POINT_SERVERCOMMAND_DISALLOW);
}

// ============================================================================
// Restart
// ============================================================================

public Action Command_Restart(int client, int args)
{
    if (client == 0)
        return Plugin_Continue;

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the reservation owner can restart.");
        return Plugin_Handled;
    }

    ShowRestartMenu(client);
    return Plugin_Handled;
}

void ShowRestartMenu(int client)
{
    Menu menu = new Menu(RestartMenuHandler);
    menu.SetTitle("Restart Options");
    menu.ExitBackButton = true;
    menu.AddItem("tournament", "Restart Tournament");
    menu.AddItem("game", "Restart Game");
    menu.AddItem("round", "Restart Round");
    menu.Display(client, 30);
}

public int RestartMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can restart.");
            return 0;
        }

        char info[32];
        menu.GetItem(param2, info, sizeof(info));

        if (StrEqual(info, "tournament"))
        {
            ServerCommand("mp_tournament_restart");
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Tournament restarted.");
        }
        else if (StrEqual(info, "game"))
        {
            ServerCommand("mp_restartgame 5");
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Game restarting in 5 seconds...");
        }
        else if (StrEqual(info, "round"))
        {
            ServerCommand("mp_restartround 5");
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Round restarting in 5 seconds...");
        }
    }
    else if (action == MenuAction_Cancel && param2 == MenuCancel_ExitBack)
    {
        if (IsOwner(param1))
            ShowOwnerAdminMenu(param1);
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
}

// ============================================================================
// Competitive Config
// ============================================================================

// Allowed config prefixes -- any cfg file starting with one of these is valid.
// Each prefix is paired with the league whose menu lists its configs. The names
// match the web panel's. AsiaFortress names its 6s configs afc_* and its
// Highlander configs af_*, so both share one league. The MGE and KOTF configs
// have no league prefix, so their entries are whole config names.
static const char g_sConfigPrefixes[][][] = {
    { "rgl_", "RGL" },
    { "etf2l_", "ETF2L" },
    { "fbtf_", "FBTF" },
    { "tfarena_", "TFArena" },
    { "ultitrio_", "Ultitrio" },
    { "ozfortress_", "ozfortress" },
    { "cltf2_", "CLTF2" },
    { "afc_", "AsiaFortress" },
    { "af_", "AsiaFortress" },
    { "1v1mge", "MGE" },
    { "2v2mge", "MGE" },
    { "kotf", "KOTF" }
};

// Main leagues are listed first in the config menu; the rest follow
// alphabetically. Keep in sync with LEAGUE_PRIORITY in
// app/services/competitive_configs.py.
static const char g_sLeaguePriority[][] = {
    "ETF2L",
    "RGL",
    "ozfortress",
    "AsiaFortress",
    "FBTF"
};

// RGL plugins to move between disabled/ and plugins/ when RGL configs are loaded.
// Excludes p4sstime and roundtimer_override (rglupdater manages roundtimer_override itself).
static const char g_sRGLPlugins[][] = {
    "config_checker.smx",
    "rglqol.smx",
    "updater.smx",
    "demo_check_no_discord.smx",
    "rglupdater.smx"
};

// RGL plugins that RGL configs load from disabled/ themselves. They stay
// loaded across map changes, so unload them with the RGL plugins.
// roundtimer_override would keep shortening the round timer under another
// league's config.
static const char g_sRGLConfigPlugins[][] = {
    "disabled/p4sstime.smx",
    "disabled/roundtimer_override.smx"
};

// SOAP DM plugins to move between disabled/ and plugins/ when competitive
// configs are loaded. soap_tournament reloads soap_tf2dm on every map start
// and unloads it only once both teams ready up in tournament mode. Without
// tournament mode it would never unload, and the map would keep its
// objectives locked and its resupply cabinets removed.
static const char g_sSOAPPlugins[][] = {
    "soap_tf2dm.smx",
    "soap_tournament.smx"
};

// soap_tf2dm settings whose change handlers open the doors and remove the
// resupply cabinets and spawn rooms. Loaded mid-round, soap_tf2dm does both
// before it reads these settings, so it skips them until the next round
// start.
static const char g_sSOAPMapConVars[][] = {
    "soap_opendoors",
    "soap_disablecabinet"
};

enum PluginMove
{
    PluginMove_Failed,
    PluginMove_InPlace,
    PluginMove_Moved
};

// Moves a plugin file into plugins/ or into plugins/disabled/. SourceMod loads
// every plugin in plugins/ again on map change, so a plugin that is only
// unloaded comes back on the next map.
PluginMove MovePlugin(const char[] plugin, bool enable)
{
    char enabledPath[PLATFORM_MAX_PATH];
    char disabledPath[PLATFORM_MAX_PATH];
    BuildPath(Path_SM, enabledPath, sizeof(enabledPath), "plugins/%s", plugin);
    BuildPath(Path_SM, disabledPath, sizeof(disabledPath), "plugins/disabled/%s", plugin);

    if (FileExists(enabledPath))
    {
        // Keep the copy in plugins/ (e.g., re-downloaded by RGL Updater).
        // Remove any copy in disabled/, which is stale and would block the
        // rename.
        if (FileExists(disabledPath))
            DeleteFile(disabledPath);

        if (enable)
            return PluginMove_InPlace;

        if (!RenameFile(disabledPath, enabledPath))
        {
            LogError("[summon] Failed to move plugin to disabled/: %s", plugin);
            return PluginMove_Failed;
        }

        LogMessage("[summon] Moved plugin to disabled/: %s", plugin);
        return PluginMove_Moved;
    }

    if (!FileExists(disabledPath))
    {
        LogError("[summon] Plugin not found in either location: %s", plugin);
        return PluginMove_Failed;
    }

    if (!enable)
        return PluginMove_InPlace;

    if (!RenameFile(enabledPath, disabledPath))
    {
        LogError("[summon] Failed to move plugin to plugins/: %s", plugin);
        return PluginMove_Failed;
    }

    LogMessage("[summon] Moved plugin to plugins/: %s", plugin);
    return PluginMove_Moved;
}

void EnableRGLPlugins()
{
    for (int i = 0; i < sizeof(g_sRGLPlugins); i++)
    {
        if (MovePlugin(g_sRGLPlugins[i], true) != PluginMove_Failed)
            ServerCommand("sm plugins load %s", g_sRGLPlugins[i]);
    }
}

void DisableRGLPlugins()
{
    // Unload all RGL plugins first to stop the Updater from re-downloading
    // files while we move them.
    for (int i = sizeof(g_sRGLPlugins) - 1; i >= 0; i--)
    {
        if (FindPluginByFile(g_sRGLPlugins[i]) != null)
            ServerCommand("sm plugins unload %s", g_sRGLPlugins[i]);
    }
    for (int i = 0; i < sizeof(g_sRGLConfigPlugins); i++)
    {
        if (FindPluginByFile(g_sRGLConfigPlugins[i]) != null)
            ServerCommand("sm plugins unload %s", g_sRGLConfigPlugins[i]);
    }
    ServerExecute();

    for (int i = 0; i < sizeof(g_sRGLPlugins); i++)
    {
        MovePlugin(g_sRGLPlugins[i], false);
    }

    // RGL configs turn the engine's map-change protection on, and a config
    // that does not set it would leave it on. Run from RCON, the unloads
    // above may not have happened yet, so the change hook could still find
    // rglqol loaded.
    DisableTVDelayMapChangeProtect();
}

void EnableSOAPPlugins()
{
    // Once enabled, SOAP is run by soap_tournament, which unloads soap_tf2dm
    // for a live match. Loading soap_tf2dm again for another config would
    // start a deathmatch mid-match, so load only what was in disabled/.
    bool loaded = false;
    for (int i = 0; i < sizeof(g_sSOAPPlugins); i++)
    {
        if (MovePlugin(g_sSOAPPlugins[i], true) == PluginMove_Moved)
        {
            ServerCommand("sm plugins load %s", g_sSOAPPlugins[i]);
            loaded = true;
        }
    }

    // Wait for the queued loads and the config to run first.
    if (loaded)
        CreateTimer(1.0, Timer_ApplySOAPMapSettings, _, TIMER_FLAG_NO_MAPCHANGE);
}

// Changes each enabled SOAP map setting and changes it back, so soap_tf2dm
// opens the doors and removes the resupply cabinets and spawn rooms now
// instead of at the next round start.
public Action Timer_ApplySOAPMapSettings(Handle timer)
{
    for (int i = 0; i < sizeof(g_sSOAPMapConVars); i++)
    {
        // Leave a disabled setting alone: turning one off makes soap_tf2dm
        // restart the game, which in tournament mode ends the ready-up period.
        ConVar cvar = FindConVar(g_sSOAPMapConVars[i]);
        if (cvar == null || cvar.IntValue < 1)
            continue;

        // Skip the chat notice for these temporary values.
        int flags = cvar.Flags;
        int value = cvar.IntValue;
        cvar.Flags = flags & ~FCVAR_NOTIFY;
        cvar.IntValue = value + 1;
        cvar.IntValue = value;
        cvar.Flags = flags;
    }
    return Plugin_Stop;
}

// Returns true if any SOAP plugin was moved to disabled/.
bool DisableSOAPPlugins()
{
    for (int i = sizeof(g_sSOAPPlugins) - 1; i >= 0; i--)
    {
        ServerCommand("sm plugins unload %s", g_sSOAPPlugins[i]);
    }
    ServerExecute();

    bool moved = false;
    for (int i = 0; i < sizeof(g_sSOAPPlugins); i++)
    {
        if (MovePlugin(g_sSOAPPlugins[i], false) == PluginMove_Moved)
            moved = true;
    }
    return moved;
}

bool IsMGEConfig(const char[] cfgFile)
{
    for (int i = 0; i < sizeof(g_sConfigPrefixes); i++)
    {
        if (StrEqual(g_sConfigPrefixes[i][1], "MGE") &&
            strncmp(cfgFile, g_sConfigPrefixes[i][0], strlen(g_sConfigPrefixes[i][0]), false) == 0)
            return true;
    }
    return false;
}

// A server starts without bots. The reset config's tf_bot_quota 0 only removes
// the bots that count toward the quota, and bot_kick needs sv_cheats.
void KickBots()
{
    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientConnected(i) || !IsFakeClient(i) || IsClientSourceTV(i) || IsClientReplay(i))
            continue;

        KickClient(i, "Removed by summon_reset");
    }
}

void ExecuteConfig(const char[] cfgFile)
{
    bool restartGame = false;

    if (g_bAutoConfig && !g_bAutoExecuting)
    {
        g_bAutoConfig = false;
        PrintToChatAll("\x01[\x07FF6600Summon\x01] Auto config is off. Use \x0799FF99!config auto\x01 to turn it back on.");
    }

    if (StrEqual(cfgFile, "summon_reset", false))
    {
        DisableRGLPlugins();
        // SOAP does not undo its changes when unloaded: the objectives stay
        // locked and the resupply cabinets and spawn rooms stay removed.
        // Restarting the game recreates the map's entities. An empty server
        // waiting in pregame recreates them anyway once players join, and a
        // restart there would wait for the players and then end the ready-up
        // period of a competitive config loaded in the meantime.
        bool soapDisabled = DisableSOAPPlugins();
        restartGame = soapDisabled && GameRules_GetRoundState() != RoundState_Pregame;
        KickBots();
    }
    else
    {
        // The RGL plugins keep acting on later configs: rglqol blocks
        // changelevel after a match and forces a map change when tv_enable
        // or sv_pure changes. Remove them for other leagues' configs.
        if (strncmp(cfgFile, "rgl_", 4, false) == 0)
        {
            EnableRGLPlugins();
        }
        else
        {
            DisableRGLPlugins();
        }
        if (IsMGEConfig(cfgFile))
        {
            // soap_tf2dm refuses to load on MGE maps, so SOAP would only
            // leave soap_tournament loading it again on every tournament
            // restart. Once both teams ready up, soap_tournament also
            // unloads SourceMod's vote and chat trigger plugins. No restart:
            // on an MGE map soap_tf2dm changed nothing, and in tournament
            // mode a restart would end the ready-up period.
            DisableSOAPPlugins();
        }
        else
        {
            // No restart here: in tournament mode it would end the ready-up
            // period. EnableSOAPPlugins applies SOAP's map changes instead.
            EnableSOAPPlugins();
        }
    }

    ServerCommand("exec %s", cfgFile);

    if (restartGame)
        ServerCommand("mp_restartgame_immediate 1");

    if (StrEqual(cfgFile, "summon_reset", false))
        strcopy(g_sCurrentConfig, sizeof(g_sCurrentConfig), "none");
    else
        strcopy(g_sCurrentConfig, sizeof(g_sCurrentConfig), cfgFile);

    g_bConfigMismatchChecked = false;
    g_bConfigMismatchReadyChecked = false;
    ScheduleConfigMismatchCheck(CONFIG_MISMATCH_LOAD_DELAY);

    SendPlayerUpdate();
}

void PrintCurrentConfig(int client)
{
    if (g_bAutoConfig)
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] Current config: \x07FFFF00%s\x01 (Auto)", g_sCurrentConfig);
    else
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] Current config: \x07FFFF00%s", g_sCurrentConfig);
}

bool IsAllowedConfig(const char[] cfgFile)
{
    if (StrEqual(cfgFile, "summon_reset", false))
    {
        return FileExists("cfg/summon_reset.cfg");
    }
    for (int i = 0; i < sizeof(g_sConfigPrefixes); i++)
    {
        if (strncmp(cfgFile, g_sConfigPrefixes[i][0], strlen(g_sConfigPrefixes[i][0]), false) == 0)
        {
            char path[PLATFORM_MAX_PATH];
            Format(path, sizeof(path), "cfg/%s.cfg", cfgFile);
            return FileExists(path);
        }
    }
    return false;
}

// Gets the league of a cfg/ file name for the config menus. Returns false if
// the file is not a selectable config.
bool GetConfigFileLeague(const char[] filename, char[] league, int leagueSize)
{
    int len = strlen(filename);
    if (len < 5 || strcmp(filename[len - 4], ".cfg") != 0)
        return false;

    // Skip base/internal configs
    if ((len >= 9 && strcmp(filename[len - 9], "_base.cfg") == 0) ||
        (len >= 11 && strcmp(filename[len - 11], "_custom.cfg") == 0) ||
        (len >= 12 && strcmp(filename[len - 12], "_common.cfg") == 0))
        return false;

    for (int i = 0; i < sizeof(g_sConfigPrefixes); i++)
    {
        if (strncmp(filename, g_sConfigPrefixes[i][0], strlen(g_sConfigPrefixes[i][0]), false) == 0)
        {
            strcopy(league, leagueSize, g_sConfigPrefixes[i][1]);
            return true;
        }
    }
    return false;
}

public Action Command_Config(int client, int args)
{
    if (client == 0)
    {
        // Server console: execute directly if valid
        if (args < 1)
        {
            PrintToServer("[summon] Usage: sm_config <config_name>");
            return Plugin_Handled;
        }
        char cfgFile[64];
        GetCmdArg(1, cfgFile, sizeof(cfgFile));
        if (IsAutoConfigArgument(cfgFile))
        {
            if (EnableAutoConfig(cfgFile))
                PrintToServer("[summon] Auto config on (%s): %s", g_sAutoLeague, g_sCurrentConfig);
            else
                PrintToServer("[summon] Auto config needs a league: sm_config auto_<league>");
        }
        else if (IsAllowedConfig(cfgFile))
        {
            ExecuteConfig(cfgFile);
            PrintToServer("[summon] Loaded config: %s", cfgFile);
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);
        }
        else
        {
            PrintToServer("[summon] Unknown config: %s", cfgFile);
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666Config not found: \x07FFFF00%s", cfgFile);
        }
        return Plugin_Handled;
    }

    if (!IsOwner(client))
    {
        PrintCurrentConfig(client);
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the reservation owner can load configs.");
        return Plugin_Handled;
    }

    if (args < 1)
    {
        // No argument -- show the current config and the league menu
        PrintCurrentConfig(client);
        ShowLeagueMenu(client);
        return Plugin_Handled;
    }

    char cfgFile[64];
    GetCmdArg(1, cfgFile, sizeof(cfgFile));

    // Owners turn Auto on with the region's league only.
    if (StrEqual(cfgFile, "auto", false))
    {
        if (!EnableAutoConfig(cfgFile))
            PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Auto config is not available on this server yet.");
        return Plugin_Handled;
    }

    if (!IsAllowedConfig(cfgFile))
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Unknown config. Use \x0799FF99!config\x01 to see available options.");
        return Plugin_Handled;
    }

    ExecuteConfig(cfgFile);
    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);

    return Plugin_Handled;
}

void ShowLeagueMenu(int client)
{
    Menu menu = new Menu(LeagueMenuHandler);
    menu.SetTitle("Select League\nCurrent: %s", g_sCurrentConfig);
    menu.ExitBackButton = true;

    // Scan cfg/ directory and collect leagues that have at least one config file
    ArrayList leagues = new ArrayList(ByteCountToCells(32));

    DirectoryListing dir = OpenDirectory("cfg");
    if (dir != null)
    {
        char filename[PLATFORM_MAX_PATH];
        char league[32];
        FileType type;
        while (dir.GetNext(filename, sizeof(filename), type))
        {
            if (type != FileType_File)
                continue;

            // Add if not already in the list
            if (GetConfigFileLeague(filename, league, sizeof(league)) && leagues.FindString(league) == -1)
            {
                leagues.PushString(league);
            }
        }
        delete dir;
    }

    leagues.SortCustom(SortLeagueStrings);

    // Listed first so they stay on the first page as leagues are added
    menu.AddItem("summon_reset", "Reset to Defaults");
    char autoLabel[64];
    char leagueName[32];
    if (GetAutoConfigLeagueName(leagueName, sizeof(leagueName)))
    {
        Format(autoLabel, sizeof(autoLabel), "Auto (%s)%s", leagueName, g_bAutoConfig ? " - on" : "");
        menu.AddItem("auto", autoLabel);
    }

    for (int i = 0; i < leagues.Length; i++)
    {
        char league[32];
        leagues.GetString(i, league, sizeof(league));
        menu.AddItem(league, league);
    }

    delete leagues;

    menu.Display(client, 30);
}

public int SortLeagueStrings(int index1, int index2, Handle array, Handle hndl)
{
    ArrayList list = view_as<ArrayList>(array);
    char str1[32], str2[32];
    list.GetString(index1, str1, sizeof(str1));
    list.GetString(index2, str2, sizeof(str2));

    int priority1 = GetLeaguePriority(str1);
    int priority2 = GetLeaguePriority(str2);
    if (priority1 != priority2)
        return priority1 - priority2;
    return strcmp(str1, str2, false);
}

int GetLeaguePriority(const char[] league)
{
    for (int i = 0; i < sizeof(g_sLeaguePriority); i++)
    {
        if (StrEqual(league, g_sLeaguePriority[i]))
            return i;
    }
    return sizeof(g_sLeaguePriority);
}

public int LeagueMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can load configs.");
            return 0;
        }

        char info[32];
        menu.GetItem(param2, info, sizeof(info));

        if (StrEqual(info, "summon_reset"))
        {
            ExecuteConfig("summon_reset");
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Config reset to Valve defaults.");
        }
        else if (StrEqual(info, "auto"))
        {
            if (!EnableAutoConfig(info))
                PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Auto config is not available on this server yet.");
        }
        else
        {
            ShowConfigMenu(param1, info);
        }
    }
    else if (action == MenuAction_Cancel && param2 == MenuCancel_ExitBack)
    {
        if (IsOwner(param1))
            ShowOwnerAdminMenu(param1);
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
}

void ShowConfigMenu(int client, const char[] league)
{
    Menu menu = new Menu(ConfigMenuHandler);
    menu.SetTitle("Select Config");
    menu.ExitBackButton = true;

    // Scan cfg/ directory for the league's .cfg files
    DirectoryListing dir = OpenDirectory("cfg");
    if (dir == null)
    {
        PrintToChat(client, "\x01[\x07FF6600Summon\x01] \x07FF6666Could not read cfg directory.");
        delete menu;
        return;
    }

    ArrayList configs = new ArrayList(ByteCountToCells(64));

    char filename[PLATFORM_MAX_PATH];
    char fileLeague[32];
    FileType type;
    while (dir.GetNext(filename, sizeof(filename), type))
    {
        if (type != FileType_File)
            continue;

        if (!GetConfigFileLeague(filename, fileLeague, sizeof(fileLeague)) || !StrEqual(fileLeague, league))
            continue;

        // Strip .cfg extension for the menu item
        int len = strlen(filename);
        char cfgName[64];
        strcopy(cfgName, sizeof(cfgName), filename);
        cfgName[len - 4] = '\0';

        configs.PushString(cfgName);
    }

    delete dir;

    // The directory order depends on the host filesystem (ext4 returns hash
    // order), so sort to match the web panel.
    configs.Sort(Sort_Ascending, Sort_String);

    for (int i = 0; i < configs.Length; i++)
    {
        char cfgName[64];
        configs.GetString(i, cfgName, sizeof(cfgName));
        menu.AddItem(cfgName, cfgName);
    }

    delete configs;
    menu.Display(client, 30);
}

public int ConfigMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Summon\x01] \x07FF6666Only the active reservation owner can load configs.");
            return 0;
        }

        char cfgFile[64];
        menu.GetItem(param2, cfgFile, sizeof(cfgFile));

        if (IsAllowedConfig(cfgFile))
        {
            ExecuteConfig(cfgFile);
            PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);
        }
    }
    else if (action == MenuAction_Cancel && param2 == MenuCancel_ExitBack)
    {
        if (IsOwner(param1))
            ShowLeagueMenu(param1);
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
}

// ============================================================================
// Config Mismatch Warning
// ============================================================================

// A league config is written for one game mode, and it stays loaded when the
// map changes, so a KOTH map can end up with a 5CP config. When the map's
// mode and the config's differ, everyone is told in chat, at most twice per
// map and config: once after players join or the config loads, and once when
// a team first readies up. Nothing is changed, since the owner may want it.

enum ConfigMode
{
    ConfigMode_Unknown,
    ConfigMode_CP,          // symmetric control points: 5CP and 3CP
    ConfigMode_KOTH,
    ConfigMode_Stopwatch,   // Attack/Defend and Payload
    ConfigMode_CTF
};

// Game modes as config names spell them, between underscores.
static const char g_sConfigModeTokens[][] = {
    "5cp",
    "3cp",
    "koth",
    "stopwatch",
    "ctf"
};

static const ConfigMode g_iConfigModeTokenModes[] = {
    ConfigMode_CP,
    ConfigMode_CP,
    ConfigMode_KOTH,
    ConfigMode_Stopwatch,
    ConfigMode_CTF
};

// Maps for formats with their own configs. They run on KOTH or CTF logic, so
// the game rules alone would report them as those modes. BBall maps are named
// ctf_bball_* or ctf_ballin_*.
static const char g_sOwnFormatMapNames[][] = {
    "bball",
    "ballin",
    "ultiduo",
    "ultitrio",
    "kotf"
};

// Finds the game mode in a config name such as etf2l_6v6_koth. start and end
// bound the mode within the name. Configs named for no mode, such as
// etf2l_ultiduo, return ConfigMode_Unknown.
ConfigMode GetConfigMode(const char[] cfgFile, int &start = 0, int &end = 0)
{
    int len = strlen(cfgFile);
    int partStart = 0;
    for (int i = 0; i <= len; i++)
    {
        if (i < len && cfgFile[i] != '_')
            continue;

        for (int t = 0; t < sizeof(g_sConfigModeTokens); t++)
        {
            int tokenLen = strlen(g_sConfigModeTokens[t]);
            if (i - partStart == tokenLen &&
                strncmp(cfgFile[partStart], g_sConfigModeTokens[t], tokenLen, false) == 0)
            {
                start = partStart;
                end = i;
                return g_iConfigModeTokenModes[t];
            }
        }
        partStart = i + 1;
    }
    return ConfigMode_Unknown;
}

// Gets the current map's game mode from the game rules, which the game sets
// from the map's entities, and its name as shown to players. Returns
// ConfigMode_Unknown for modes no config is named for, and for maps this
// cannot tell apart.
ConfigMode GetMapMode(char[] name, int nameSize)
{
    char map[PLATFORM_MAX_PATH];
    GetCurrentMap(map, sizeof(map));
    for (int i = 0; i < sizeof(g_sOwnFormatMapNames); i++)
    {
        if (StrContains(map, g_sOwnFormatMapNames[i], false) != -1)
            return ConfigMode_Unknown;
    }

    if (FindEntityByClassname(-1, "tf_gamerules") == -1)
        return ConfigMode_Unknown;

    if (GameRules_GetProp("m_bPlayingHybrid_CTF_CP") || GameRules_GetProp("m_bPlayingSpecialDeliveryMode"))
        return ConfigMode_Unknown;

    if (GameRules_GetProp("m_bPlayingKoth"))
    {
        strcopy(name, nameSize, "KOTH");
        return ConfigMode_KOTH;
    }

    switch (GameRules_GetProp("m_nGameType"))
    {
        case TF_GAMETYPE_CTF:
        {
            strcopy(name, nameSize, "CTF");
            return ConfigMode_CTF;
        }
        case TF_GAMETYPE_ESCORT:
        {
            // Payload Race has two carts and is not played in stopwatch mode.
            if (GameRules_GetProp("m_bMultipleTrains"))
                return ConfigMode_Unknown;

            strcopy(name, nameSize, "Payload");
            return ConfigMode_Stopwatch;
        }
        case TF_GAMETYPE_CP:
        {
            // On an Attack/Defend map RED starts with every point. The
            // default owner is the map's own value and does not change as
            // points are captured.
            int points = 0;
            int redPoints = 0;
            int ent = -1;
            while ((ent = FindEntityByClassname(ent, "team_control_point")) != -1)
            {
                points++;
                if (GetEntProp(ent, Prop_Data, "m_iDefaultOwner") == TF_TEAM_RED)
                    redPoints++;
            }

            if (points == 0)
                return ConfigMode_Unknown;

            if (redPoints == points)
            {
                strcopy(name, nameSize, "Attack/Defend");
                return ConfigMode_Stopwatch;
            }

            Format(name, nameSize, "%dCP", points);
            return ConfigMode_CP;
        }
    }
    return ConfigMode_Unknown;
}

// Finds the config for another mode in the same league and format as cfgFile,
// whose mode lies between start and end. The rest of the name is kept if such
// a config exists (rgl_6s_5cp_scrim -> rgl_6s_koth_scrim) and dropped
// otherwise (etf2l_9v9_koth_spooky -> etf2l_9v9_5cp). Returns false when
// neither exists: RGL's 6s 5CP configs, for one, all name a scrim or match
// type that a KOTH config name does not give.
bool FindConfigForMode(const char[] cfgFile, int start, int end, ConfigMode mode, char[] suggestion, int suggestionSize)
{
    char prefix[64];
    char rest[64];
    strcopy(prefix, sizeof(prefix), cfgFile);
    prefix[start] = '\0';
    strcopy(rest, sizeof(rest), cfgFile[end]);

    for (int attempt = 0; attempt < 2; attempt++)
    {
        for (int t = 0; t < sizeof(g_sConfigModeTokens); t++)
        {
            if (g_iConfigModeTokenModes[t] != mode)
                continue;

            Format(suggestion, suggestionSize, "%s%s%s", prefix, g_sConfigModeTokens[t], rest);
            if (IsAllowedConfig(suggestion))
                return true;
        }

        if (rest[0] == '\0')
            break;
        rest[0] = '\0';
    }

    suggestion[0] = '\0';
    return false;
}

bool IsAnyHumanInGame()
{
    for (int i = 1; i <= MaxClients; i++)
    {
        if (IsClientInGame(i) && !IsFakeClient(i))
            return true;
    }
    return false;
}

// Warns everyone if the loaded config is for another game mode than the
// current map. Returns false if no player is there to read it.
bool CheckConfigMismatch()
{
    if (!IsAnyHumanInGame())
        return false;

    int start, end;
    ConfigMode configMode = GetConfigMode(g_sCurrentConfig, start, end);
    if (configMode == ConfigMode_Unknown)
        return true;

    char mapModeName[32];
    ConfigMode mapMode = GetMapMode(mapModeName, sizeof(mapModeName));
    if (mapMode == ConfigMode_Unknown || mapMode == configMode)
        return true;

    char map[PLATFORM_MAX_PATH];
    char mapName[PLATFORM_MAX_PATH];
    GetCurrentMap(map, sizeof(map));
    if (!GetMapDisplayName(map, mapName, sizeof(mapName)))
        strcopy(mapName, sizeof(mapName), map);

    PrintToChatAll(
        "\x01[\x07FF6600Summon\x01] \x07FF6666Config mismatch: \x07FFFF00%s\x01 is %s, but the config is \x07FFFF00%s\x01.",
        mapName,
        mapModeName,
        g_sCurrentConfig
    );

    char suggestion[64];
    if (FindConfigForMode(g_sCurrentConfig, start, end, mapMode, suggestion, sizeof(suggestion)))
    {
        PrintToChatAll(
            "\x01[\x07FF6600Summon\x01] The reservation owner can load \x0799FF99!config %s\x01 instead.",
            suggestion
        );
    }
    else
    {
        PrintToChatAll("\x01[\x07FF6600Summon\x01] The reservation owner can load another with \x0799FF99!config\x01.");
    }
    return true;
}

void ScheduleConfigMismatchCheck(float delay)
{
    if (g_hConfigMismatchTimer != INVALID_HANDLE)
    {
        KillTimer(g_hConfigMismatchTimer);
    }
    g_hConfigMismatchTimer = CreateTimer(delay, Timer_ConfigMismatch, _, TIMER_FLAG_NO_MAPCHANGE);
}

public Action Timer_ConfigMismatch(Handle timer)
{
    g_hConfigMismatchTimer = INVALID_HANDLE;
    // A team that readied up in the meantime has had the warning already.
    // With nobody on the server, the next player to join starts the check
    // again.
    if (!g_bConfigMismatchChecked && CheckConfigMismatch())
        g_bConfigMismatchChecked = true;
    return Plugin_Stop;
}

public void Event_TournamentStateUpdate(Event event, const char[] name, bool dontBroadcast)
{
    // The event also reports team name changes and teams that unready.
    if (event.GetBool("namechange") || event.GetInt("readystate") == 0)
        return;

    if (g_bConfigMismatchReadyChecked)
        return;

    // The last moment a wrong config is cheap to fix. This also stands in for
    // a check still waiting on its timer.
    g_bConfigMismatchReadyChecked = true;
    g_bConfigMismatchChecked = true;
    CheckConfigMismatch();
}

// ============================================================================
// Auto Config
// ============================================================================

// With Auto on, Summon picks the config: the league's config for the map's
// game mode, as 6v6 until both teams have AUTO_HIGHLANDER_TEAM_SIZE players
// and as Highlander from then on. It loads a new one at each map start, and
// once when the teams grow, but never during a live match, since league
// configs restart the tournament. The league comes from the server's region;
// the backend sends "sm_config auto_<league>" or names it in its reply to
// player updates.

// League keys, as the backend sends them, and their names in the menus.
static const char g_sAutoLeagueNames[][][] = {
    { "etf2l", "ETF2L" },
    { "rgl", "RGL" },
    { "ozfortress", "ozfortress" }
};

// The config Auto loads for each league and kind of map. Kinds are the format
// and the mode ("6v6_koth", "hl_stopwatch"), or a map's own format
// ("ultiduo"). RGL's 6s 5CP and KOTH configs come in scrim and match
// versions; a reservation is for scrims. A kind a league has no config for
// keeps the loaded config, and the mismatch warning says so.
static const char g_sAutoConfigs[][][] = {
    { "etf2l", "6v6_cp", "etf2l_6v6_5cp" },
    { "etf2l", "6v6_koth", "etf2l_6v6_koth" },
    { "etf2l", "6v6_stopwatch", "etf2l_6v6_stopwatch" },
    { "etf2l", "6v6_ctf", "etf2l_6v6_ctf" },
    { "etf2l", "hl_cp", "etf2l_9v9_5cp" },
    { "etf2l", "hl_koth", "etf2l_9v9_koth" },
    { "etf2l", "hl_stopwatch", "etf2l_9v9_stopwatch" },
    { "etf2l", "hl_ctf", "etf2l_9v9_ctf" },
    { "etf2l", "ultiduo", "etf2l_ultiduo" },
    { "etf2l", "bball", "etf2l_bball" },
    { "rgl", "6v6_cp", "rgl_6s_5cp_scrim" },
    { "rgl", "6v6_koth", "rgl_6s_koth_scrim" },
    { "rgl", "6v6_stopwatch", "rgl_6s_stopwatch" },
    { "rgl", "hl_cp", "rgl_HL_5cp_scrim" },
    { "rgl", "hl_koth", "rgl_HL_koth" },
    { "rgl", "hl_stopwatch", "rgl_HL_stopwatch" },
    { "rgl", "ultiduo", "rgl_ud_ultiduo" },
    { "ozfortress", "6v6_cp", "ozfortress_6v6_5cp" },
    { "ozfortress", "6v6_koth", "ozfortress_6v6_koth" },
    { "ozfortress", "hl_cp", "ozfortress_hl_5cp" },
    { "ozfortress", "hl_koth", "ozfortress_hl_koth" },
    { "ozfortress", "hl_stopwatch", "ozfortress_hl_stopwatch" },
    { "ozfortress", "ultiduo", "ozfortress_ultiduo" },
    { "ozfortress", "bball", "ozfortress_bball" }
};

bool IsAutoConfigLeague(const char[] league)
{
    for (int i = 0; i < sizeof(g_sAutoLeagueNames); i++)
    {
        if (StrEqual(league, g_sAutoLeagueNames[i][0]))
            return true;
    }
    return false;
}

// Gets the menu name of Auto's league. Returns false while it is unknown.
bool GetAutoConfigLeagueName(char[] name, int nameSize)
{
    for (int i = 0; i < sizeof(g_sAutoLeagueNames); i++)
    {
        if (StrEqual(g_sAutoLeague, g_sAutoLeagueNames[i][0]))
        {
            strcopy(name, nameSize, g_sAutoLeagueNames[i][1]);
            return true;
        }
    }
    return false;
}

// "auto" or "auto_<league>". No config file is named like this: configs need
// a league prefix.
bool IsAutoConfigArgument(const char[] arg)
{
    return StrEqual(arg, "auto", false) || strncmp(arg, "auto_", 5, false) == 0;
}

// Gets the kind of the current map for g_sAutoConfigs, and its mode as shown
// to players. formatBased is false for maps with their own format, which
// Highlander does not change. Returns false for maps Auto has no kind for.
bool GetAutoConfigKind(char[] kind, int kindSize, char[] modeName, int modeNameSize, bool &formatBased)
{
    char map[PLATFORM_MAX_PATH];
    GetCurrentMap(map, sizeof(map));

    formatBased = false;
    if (StrContains(map, "ultiduo", false) != -1)
    {
        strcopy(kind, kindSize, "ultiduo");
        strcopy(modeName, modeNameSize, "Ultiduo");
        return true;
    }
    if (StrContains(map, "bball", false) != -1 || StrContains(map, "ballin", false) != -1)
    {
        strcopy(kind, kindSize, "bball");
        strcopy(modeName, modeNameSize, "BBall");
        return true;
    }

    // GetMapMode leaves the other maps with their own format, such as
    // Ultitrio and KOTF, as unknown.
    ConfigMode mode = GetMapMode(modeName, modeNameSize);
    char modeToken[16];
    switch (mode)
    {
        case ConfigMode_CP: strcopy(modeToken, sizeof(modeToken), "cp");
        case ConfigMode_KOTH: strcopy(modeToken, sizeof(modeToken), "koth");
        case ConfigMode_Stopwatch: strcopy(modeToken, sizeof(modeToken), "stopwatch");
        case ConfigMode_CTF: strcopy(modeToken, sizeof(modeToken), "ctf");
        default: return false;
    }

    formatBased = true;
    Format(kind, kindSize, "%s_%s", g_bAutoHighlander ? "hl" : "6v6", modeToken);
    return true;
}

// Finds Auto's config for a kind of map. Returns false if the league has none
// or the server lacks the file.
bool FindAutoConfig(const char[] kind, char[] cfgFile, int cfgFileSize)
{
    for (int i = 0; i < sizeof(g_sAutoConfigs); i++)
    {
        if (StrEqual(g_sAutoConfigs[i][0], g_sAutoLeague) && StrEqual(g_sAutoConfigs[i][1], kind))
        {
            if (!IsAllowedConfig(g_sAutoConfigs[i][2]))
                break;
            strcopy(cfgFile, cfgFileSize, g_sAutoConfigs[i][2]);
            return true;
        }
    }
    cfgFile[0] = '\0';
    return false;
}

// Loads Auto's config for the current map unless it is loaded already.
// Returns true if it loaded one. With announce, says what Auto did even if it
// changed nothing, for when it is turned on.
bool ApplyAutoConfig(bool announce)
{
    char kind[32];
    char modeName[32];
    char cfgFile[64];
    bool formatBased;
    bool found = GetAutoConfigKind(kind, sizeof(kind), modeName, sizeof(modeName), formatBased)
        && FindAutoConfig(kind, cfgFile, sizeof(cfgFile));

    if (!found)
    {
        char leagueName[32];
        GetAutoConfigLeagueName(leagueName, sizeof(leagueName));
        LogMessage("[summon] Auto config: no %s config for this map; keeping %s", leagueName, g_sCurrentConfig);
        if (announce)
        {
            PrintToChatAll(
                "\x01[\x07FF6600Summon\x01] \x0799FF99Auto config is on.\x01 No %s config suits this map, so \x07FFFF00%s\x01 stays loaded.",
                leagueName,
                g_sCurrentConfig
            );
        }
        return false;
    }

    if (StrEqual(cfgFile, g_sCurrentConfig))
    {
        if (announce)
        {
            PrintToChatAll(
                "\x01[\x07FF6600Summon\x01] \x0799FF99Auto config is on:\x01 \x07FFFF00%s\x01 suits this map.",
                cfgFile
            );
        }
        return false;
    }

    g_bAutoExecuting = true;
    ExecuteConfig(cfgFile);
    g_bAutoExecuting = false;
    LogMessage("[summon] Auto config: loaded %s (%s)", cfgFile, kind);

    if (announce)
    {
        PrintToChatAll(
            "\x01[\x07FF6600Summon\x01] \x0799FF99Auto config is on:\x01 loaded \x07FFFF00%s\x01 for %s.",
            cfgFile,
            modeName
        );
    }
    else
    {
        PrintToChatAll(
            "\x01[\x07FF6600Summon\x01] \x0799FF99Auto config:\x01 loaded \x07FFFF00%s\x01 for %s.",
            cfgFile,
            modeName
        );
    }
    return true;
}

bool BothTeamsAtHighlanderSize()
{
    return GetTeamClientCount(TF_TEAM_RED) >= AUTO_HIGHLANDER_TEAM_SIZE
        && GetTeamClientCount(TF_TEAM_BLUE) >= AUTO_HIGHLANDER_TEAM_SIZE;
}

// Turns Auto on. arg is "auto", which needs the league to be known, or
// "auto_<league>". Returns false if the league is unknown.
bool EnableAutoConfig(const char[] arg)
{
    if (strncmp(arg, "auto_", 5, false) == 0)
    {
        char league[sizeof(g_sAutoLeague)];
        strcopy(league, sizeof(league), arg[5]);
        if (!IsAutoConfigLeague(league))
            return false;
        strcopy(g_sAutoLeague, sizeof(g_sAutoLeague), league);
    }
    else if (!IsAutoConfigLeague(g_sAutoLeague))
    {
        return false;
    }

    // Turning Auto on again starts over from the teams as they are now.
    g_bAutoConfig = true;
    g_bAutoHighlander = BothTeamsAtHighlanderSize();
    if (!ApplyAutoConfig(true))
        SendPlayerUpdate();

    PrintToChatAll(
        "\x01[\x07FF6600Summon\x01] Auto config follows the map, and switches to Highlander once both teams have %d or more players.",
        AUTO_HIGHLANDER_TEAM_SIZE
    );
    return true;
}

// Switches Auto to Highlander once both teams are big enough. Called every
// second outside live matches.
void CheckAutoHighlander()
{
    if (!g_bAutoConfig || g_bAutoHighlander || !BothTeamsAtHighlanderSize())
        return;

    char kind[32];
    char modeName[32];
    bool formatBased;
    if (GetAutoConfigKind(kind, sizeof(kind), modeName, sizeof(modeName), formatBased) && !formatBased)
        return;

    g_bAutoHighlander = true;
    PrintToChatAll(
        "\x01[\x07FF6600Summon\x01] \x0799FF99Both teams have %d or more players,\x01 so Auto config switches to Highlander.",
        AUTO_HIGHLANDER_TEAM_SIZE
    );
    ApplyAutoConfig(false);
}

// ============================================================================
// RCON Commands (triggered by Go agent)
// ============================================================================

public Action Command_ReservationWarning(int args)
{
    if (args < 1)
    {
        return Plugin_Handled;
    }

    char arg[16];
    GetCmdArg(1, arg, sizeof(arg));
    int minutes = StringToInt(arg);

    if (minutes == 1)
    {
        PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FF6666This reservation ends in 1 minute!");
    }
    else
    {
        PrintToChatAll("\x01[\x07FF6600Summon\x01] \x07FFFF00This reservation ends in %d minutes.", minutes);
    }

    return Plugin_Handled;
}

public Action Command_ReservationEnding(int args)
{
    PrintToChatAll("\x01[\x07FF6600Summon\x01] \x0799FF99Reservation ending now. Thanks for playing!");
    CreateTimer(5.0, Timer_KickAll);

    return Plugin_Handled;
}

// ============================================================================
// Upload Link Reporting (logs.tf / demos.tf)
// ============================================================================

public void LogUploaded(bool success, const char[] logid, const char[] url)
{
    if (!success || strlen(logid) == 0 || strlen(url) == 0)
        return;

    ReportUploadLink("log", logid, url);
}

public int DemoUploaded(bool success, const char[] demoid, const char[] url)
{
    if (!success || strlen(demoid) == 0 || strlen(url) == 0)
        return 0;

    ReportUploadLink("demo", demoid, url);
    return 0;
}

void ReportUploadLink(const char[] type, const char[] externalId, const char[] uploadUrl)
{
    char backendURL[256];
    char apiKey[64];

    g_cvBackendURL.GetString(backendURL, sizeof(backendURL));
    g_cvAPIKey.GetString(apiKey, sizeof(apiKey));

    int reservationNumber = g_cvReservationNumber.IntValue;

    if (strlen(backendURL) == 0 || strlen(apiKey) == 0 || reservationNumber == 0)
        return;

    // Normalize URL to https
    char normalizedUrl[256];
    if (strncmp(uploadUrl, "http://", 7) == 0)
    {
        Format(normalizedUrl, sizeof(normalizedUrl), "https://%s", uploadUrl[7]);
    }
    else
    {
        strcopy(normalizedUrl, sizeof(normalizedUrl), uploadUrl);
    }

    // Build payload
    JSONObject body = new JSONObject();
    body.SetString("type", type);
    body.SetString("external_id", externalId);
    body.SetString("url", normalizedUrl);

    // Send HTTP POST
    char reqUrl[512];
    Format(reqUrl, sizeof(reqUrl), "%s/internal/reservations/%d/uploads", backendURL, reservationNumber);

    HTTPRequest request = new HTTPRequest(reqUrl);
    request.SetHeader("X-API-Key", apiKey);
    request.SetHeader("Content-Type", "application/json");
    request.Post(body, OnUploadLinkResponse);
    delete body;
}

public void OnUploadLinkResponse(HTTPResponse response, any data, const char[] error)
{
    if (strlen(error) > 0)
    {
        LogError("[summon] Failed to report upload link: %s", error);
        return;
    }

    if (response.Status != HTTPStatus_OK)
    {
        LogError("[summon] Backend returned HTTP %d for upload link", response.Status);
    }
}

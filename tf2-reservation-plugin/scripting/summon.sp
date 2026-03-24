/**
 * Summon - SourceMod Reservation Plugin
 *
 * Provides in-game reservation management for TF2 servers
 * Commands: !reservation, !end, !cancel, !changemap, !restart
 */

#include <sourcemod>
#include <ripext>

#pragma semicolon 1
#pragma newdecls required

#undef REQUIRE_PLUGIN
#include <logstf>
#include <demostf>
#define REQUIRE_PLUGIN

#define PLUGIN_VERSION "1.1.1"
#define PLUGIN_NAME "Summon"
#define PLAYER_UPDATE_INTERVAL 10.0
#define PLAYER_JOIN_REFRESH_DELAY 3.0

// ConVars - set by agent RCON at boot
ConVar g_cvOwnerSteamID;
ConVar g_cvOwnerName;
ConVar g_cvReservationNumber;
ConVar g_cvEndsAt;
ConVar g_cvBackendURL;
ConVar g_cvAPIKey;

// Forward
GlobalForward g_fwdOnMapChangeRequested;

// State
bool g_bEndCountdownActive = false;
Handle g_hEndTimer = INVALID_HANDLE;
Handle g_hPlayerUpdateTimer = INVALID_HANDLE;
Handle g_hExpiryTimer = INVALID_HANDLE;
int g_iEndCountdown = 0;
bool g_bExpiryKickDone = false;

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

    // Register chat commands
    RegConsoleCmd("sm_reservation", Command_Reservation, "Show reservation info");
    RegConsoleCmd("sm_end", Command_End, "End the reservation");
    RegConsoleCmd("sm_cancel", Command_Cancel, "Cancel pending reservation end");
    RegConsoleCmd("sm_changemap", Command_Map, "Change the map (owner only)");
    RegConsoleCmd("sm_config", Command_Config, "Load a competitive config (owner only)");
    RegConsoleCmd("sm_restart", Command_Restart, "Restart tournament/game/round (owner only)");

    // Register RCON commands (called by agent)
    RegServerCmd("sm_reservation_warning", Command_ReservationWarning);
    RegServerCmd("sm_reservation_ending", Command_ReservationEnding);
    LogMessage("[summon] Plugin loaded v%s", PLUGIN_VERSION);
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
}

public void OnClientPostAdminCheck(int client)
{
    if (IsFakeClient(client))
        return;

    SendPlayerUpdate();
    CreateTimer(PLAYER_JOIN_REFRESH_DELAY, Timer_SendPlayerUpdate, _, TIMER_FLAG_NO_MAPCHANGE);
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

    // Build payload
    JSONObject body = new JSONObject();
    body.SetInt("player_count", playerCount);
    body.Set("players", playersArr);
    delete playersArr;

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
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

bool IsOwner(int client)
{
    char clientSteamID[32];
    char ownerSteamID[32];

    if (!GetClientAuthId(client, AuthId_SteamID64, clientSteamID, sizeof(clientSteamID)))
    {
        return false;
    }

    g_cvOwnerSteamID.GetString(ownerSteamID, sizeof(ownerSteamID));

    return StrEqual(clientSteamID, ownerSteamID, false);
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
        case 10800: PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 3 hours.");
        case 7200:  PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 2 hours.");
        case 3600:  PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 1 hour.");
        case 1800:  PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 30 minutes.");
        case 900:   PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 15 minutes.");
        case 300:   PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in 5 minutes.");
        case 60:    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 1 minute!");
        case 30:    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 30 seconds!");
        case 20:    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 20 seconds!");
        case 10:    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 10 seconds!");
        case 5:     PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 5 seconds!");
        case 4:     PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 4 seconds!");
        case 3:     PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 3 seconds!");
        case 2:     PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 2 seconds!");
        case 1:     PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 1 second!");
    }

    // Kick all players when time expires
    if (remaining <= 0 && !g_bExpiryKickDone)
    {
        g_bExpiryKickDone = true;
        PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Reservation has expired. Thanks for playing!");

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

public Action Command_Reservation(int client, int args)
{
    char ownerName[64];
    char timeRemaining[64];

    g_cvOwnerName.GetString(ownerName, sizeof(ownerName));
    int reservationNumber = g_cvReservationNumber.IntValue;
    int remaining = GetTimeRemaining();

    FormatTimeRemaining(remaining, timeRemaining, sizeof(timeRemaining));

    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x0799FF99Reservation #%d", reservationNumber);
    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Reserved by: \x0799FF99%s", ownerName);
    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Time remaining: \x0799FF99%s", timeRemaining);

    return Plugin_Handled;
}

public Action Command_End(int client, int args)
{
    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can end the reservation.");
        return Plugin_Handled;
    }

    if (g_bEndCountdownActive)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] End countdown already in progress. Use \x0799FF99!cancel\x01 to abort.");
        return Plugin_Handled;
    }

    // Start 30-second countdown
    g_bEndCountdownActive = true;
    g_iEndCountdown = 30;

    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00Reservation ending in 30 seconds!");
    PrintToChatAll("\x01[\x07FF6600Reserve\x01] Type \x0799FF99!cancel\x01 to abort.");

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

    // Show countdown at 20, 10, 5, 4, 3, 2, 1
    if (g_iEndCountdown == 20 || g_iEndCountdown == 10 || g_iEndCountdown <= 5)
    {
        PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00Reservation ending in %d seconds...", g_iEndCountdown);
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
    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Thanks for playing!");

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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can cancel.");
        return Plugin_Handled;
    }

    if (!g_bEndCountdownActive)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] No end countdown active.");
        return Plugin_Handled;
    }

    g_bEndCountdownActive = false;

    if (g_hEndTimer != INVALID_HANDLE)
    {
        KillTimer(g_hEndTimer);
        g_hEndTimer = INVALID_HANDLE;
    }

    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Reservation end cancelled.");

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

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can change the map.");
        return Plugin_Handled;
    }

    if (args < 1)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Usage: !changemap <mapname>");
        return Plugin_Handled;
    }

    // Fire forward so mapdownloader (or other plugins) can act on it
    char mapName[PLATFORM_MAX_PATH];
    GetCmdArg(1, mapName, sizeof(mapName));

    Action result;
    Call_StartForward(g_fwdOnMapChangeRequested);
    Call_PushCell(client);
    Call_PushString(mapName);
    Call_Finish(result);

    if (result >= Plugin_Handled)
    {
        // A listener blocked it (shouldn't normally happen after owner check,
        // but allows other plugins to veto if needed)
        return Plugin_Handled;
    }

    return Plugin_Handled;
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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can restart.");
        return Plugin_Handled;
    }

    ShowRestartMenu(client);
    return Plugin_Handled;
}

void ShowRestartMenu(int client)
{
    Menu menu = new Menu(RestartMenuHandler);
    menu.SetTitle("Restart Options");
    menu.AddItem("tournament", "Restart Tournament");
    menu.AddItem("game", "Restart Game");
    menu.AddItem("round", "Restart Round");
    menu.Display(client, 30);
}

public int RestartMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        char info[32];
        menu.GetItem(param2, info, sizeof(info));

        if (StrEqual(info, "tournament"))
        {
            ServerCommand("mp_tournament_restart");
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Tournament restarted.");
        }
        else if (StrEqual(info, "game"))
        {
            ServerCommand("mp_restartgame 5");
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Game restarting in 5 seconds...");
        }
        else if (StrEqual(info, "round"))
        {
            ServerCommand("mp_restartround 5");
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Round restarting in 5 seconds...");
        }
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

// Allowed config prefixes -- any cfg file starting with one of these is valid
static const char g_sAllowedPrefixes[][] = {
    "rgl_",
    "etf2l_",
    "fbtf_",
    "tfarena_",
    "ultitrio_",
    "ozfortress_",
    "cltf2_"
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

void EnableRGLPlugins()
{
    char src[PLATFORM_MAX_PATH];
    char dst[PLATFORM_MAX_PATH];

    for (int i = 0; i < sizeof(g_sRGLPlugins); i++)
    {
        BuildPath(Path_SM, src, sizeof(src), "plugins/disabled/%s", g_sRGLPlugins[i]);
        BuildPath(Path_SM, dst, sizeof(dst), "plugins/%s", g_sRGLPlugins[i]);

        if (FileExists(dst))
        {
            // Already in plugins/ (e.g., re-downloaded by RGL Updater).
            // Remove stale disabled/ copy if present.
            if (FileExists(src))
                DeleteFile(src);
        }
        else if (FileExists(src))
        {
            if (RenameFile(dst, src))
            {
                LogMessage("[summon] Moved RGL plugin to plugins/: %s", g_sRGLPlugins[i]);
            }
            else
            {
                LogError("[summon] Failed to move RGL plugin: %s", g_sRGLPlugins[i]);
                continue;
            }
        }
        else
        {
            LogError("[summon] RGL plugin not found in either location: %s", g_sRGLPlugins[i]);
            continue;
        }

        ServerCommand("sm plugins load %s", g_sRGLPlugins[i]);
    }
}

void DisableRGLPlugins()
{
    char src[PLATFORM_MAX_PATH];
    char dst[PLATFORM_MAX_PATH];

    // Unload all RGL plugins first to stop the Updater from re-downloading
    // files while we move them.
    for (int i = sizeof(g_sRGLPlugins) - 1; i >= 0; i--)
    {
        ServerCommand("sm plugins unload %s", g_sRGLPlugins[i]);
    }
    ServerExecute();

    for (int i = 0; i < sizeof(g_sRGLPlugins); i++)
    {
        BuildPath(Path_SM, src, sizeof(src), "plugins/%s", g_sRGLPlugins[i]);
        BuildPath(Path_SM, dst, sizeof(dst), "plugins/disabled/%s", g_sRGLPlugins[i]);

        if (!FileExists(src))
            continue;

        // Remove any existing copy in disabled/ so the rename succeeds.
        if (FileExists(dst))
            DeleteFile(dst);

        if (RenameFile(dst, src))
        {
            LogMessage("[summon] Moved RGL plugin to disabled/: %s", g_sRGLPlugins[i]);
        }
        else
        {
            LogError("[summon] Failed to move RGL plugin: %s", g_sRGLPlugins[i]);
        }
    }
}

void ExecuteConfig(const char[] cfgFile)
{
    if (strncmp(cfgFile, "rgl_", 4, false) == 0)
    {
        EnableRGLPlugins();
    }
    else if (StrEqual(cfgFile, "summon_reset", false))
    {
        DisableRGLPlugins();
    }

    ServerCommand("exec %s", cfgFile);
}

bool IsAllowedConfig(const char[] cfgFile)
{
    if (StrEqual(cfgFile, "summon_reset", false))
    {
        return FileExists("cfg/summon_reset.cfg");
    }
    for (int i = 0; i < sizeof(g_sAllowedPrefixes); i++)
    {
        if (strncmp(cfgFile, g_sAllowedPrefixes[i], strlen(g_sAllowedPrefixes[i]), false) == 0)
        {
            char path[PLATFORM_MAX_PATH];
            Format(path, sizeof(path), "cfg/%s.cfg", cfgFile);
            return FileExists(path);
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
        if (IsAllowedConfig(cfgFile))
        {
            ExecuteConfig(cfgFile);
            PrintToServer("[summon] Loaded config: %s", cfgFile);
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);
        }
        else
        {
            PrintToServer("[summon] Unknown config: %s", cfgFile);
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666Config not found: \x07FFFF00%s", cfgFile);
        }
        return Plugin_Handled;
    }

    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can load configs.");
        return Plugin_Handled;
    }

    if (args < 1)
    {
        // No argument -- show league menu
        ShowLeagueMenu(client);
        return Plugin_Handled;
    }

    char cfgFile[64];
    GetCmdArg(1, cfgFile, sizeof(cfgFile));

    if (!IsAllowedConfig(cfgFile))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Unknown config. Use \x0799FF99!config\x01 to see available options.");
        return Plugin_Handled;
    }

    ExecuteConfig(cfgFile);
    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);

    return Plugin_Handled;
}

void ShowLeagueMenu(int client)
{
    Menu menu = new Menu(LeagueMenuHandler);
    menu.SetTitle("Select League");

    // Scan cfg/ directory and collect leagues that have at least one config file
    ArrayList leagues = new ArrayList(ByteCountToCells(32));

    DirectoryListing dir = OpenDirectory("cfg");
    if (dir != null)
    {
        char filename[PLATFORM_MAX_PATH];
        FileType type;
        while (dir.GetNext(filename, sizeof(filename), type))
        {
            if (type != FileType_File)
                continue;

            int len = strlen(filename);
            if (len < 5 || strcmp(filename[len - 4], ".cfg") != 0)
                continue;

            // Skip base/internal configs
            if ((len >= 9 && strcmp(filename[len - 9], "_base.cfg") == 0) ||
                (len >= 11 && strcmp(filename[len - 11], "_custom.cfg") == 0) ||
                (len >= 12 && strcmp(filename[len - 12], "_common.cfg") == 0))
                continue;

            // Check if it matches any allowed prefix
            for (int i = 0; i < sizeof(g_sAllowedPrefixes); i++)
            {
                if (strncmp(filename, g_sAllowedPrefixes[i], strlen(g_sAllowedPrefixes[i]), false) == 0)
                {
                    // Extract league name (prefix without trailing _)
                    char league[32];
                    strcopy(league, sizeof(league), g_sAllowedPrefixes[i]);
                    league[strlen(league) - 1] = '\0';

                    // Add if not already in the list
                    if (leagues.FindString(league) == -1)
                    {
                        leagues.PushString(league);
                    }
                    break;
                }
            }
        }
        delete dir;
    }

    // Sort alphabetically
    leagues.SortCustom(SortLeagueStrings);

    for (int i = 0; i < leagues.Length; i++)
    {
        char league[32];
        leagues.GetString(i, league, sizeof(league));
        menu.AddItem(league, league);
    }

    delete leagues;

    menu.AddItem("summon_reset", "Reset to Defaults");
    menu.Display(client, 30);
}

public int SortLeagueStrings(int index1, int index2, Handle array, Handle hndl)
{
    ArrayList list = view_as<ArrayList>(array);
    char str1[32], str2[32];
    list.GetString(index1, str1, sizeof(str1));
    list.GetString(index2, str2, sizeof(str2));
    return strcmp(str1, str2, false);
}

public int LeagueMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        char info[32];
        menu.GetItem(param2, info, sizeof(info));

        if (StrEqual(info, "summon_reset"))
        {
            ExecuteConfig("summon_reset");
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Config reset to Valve defaults.");
        }
        else
        {
            ShowConfigMenu(param1, info);
        }
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

    // Build prefix to match (e.g. "rgl_", "etf2l_", "fbtf_")
    char prefix[32];
    Format(prefix, sizeof(prefix), "%s_", league);

    // Scan cfg/ directory for matching .cfg files
    DirectoryListing dir = OpenDirectory("cfg");
    if (dir == null)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Could not read cfg directory.");
        delete menu;
        return;
    }

    char filename[PLATFORM_MAX_PATH];
    FileType type;
    while (dir.GetNext(filename, sizeof(filename), type))
    {
        if (type != FileType_File)
            continue;

        // Must start with the league prefix
        if (strncmp(filename, prefix, strlen(prefix), false) != 0)
            continue;

        // Must end with .cfg
        int len = strlen(filename);
        if (len < 5 || strcmp(filename[len - 4], ".cfg") != 0)
            continue;

        // Skip summon_reset (handled separately as Reset)
        if (strncmp(filename, "summon_reset", 12, false) == 0)
            continue;

        // Skip base/internal configs (*_base.cfg, *_custom.cfg)
        if ((len >= 9 && strcmp(filename[len - 9], "_base.cfg") == 0) ||
            (len >= 11 && strcmp(filename[len - 11], "_custom.cfg") == 0))
            continue;

        // Strip .cfg extension for the menu item
        char cfgName[64];
        strcopy(cfgName, sizeof(cfgName), filename);
        cfgName[len - 4] = '\0';

        menu.AddItem(cfgName, cfgName);
    }

    delete dir;
    menu.Display(client, 30);
}

public int ConfigMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        char cfgFile[64];
        menu.GetItem(param2, cfgFile, sizeof(cfgFile));

        if (IsAllowedConfig(cfgFile))
        {
            ExecuteConfig(cfgFile);
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);
        }
    }
    else if (action == MenuAction_End)
    {
        delete menu;
    }
    return 0;
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
        PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FF6666This reservation ends in 1 minute!");
    }
    else
    {
        PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00This reservation ends in %d minutes.", minutes);
    }

    return Plugin_Handled;
}

public Action Command_ReservationEnding(int args)
{
    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Reservation ending now. Thanks for playing!");
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

public void DemoUploaded(bool success, const char[] demoid, const char[] url)
{
    if (!success || strlen(demoid) == 0 || strlen(url) == 0)
        return;

    ReportUploadLink("demo", demoid, url);
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

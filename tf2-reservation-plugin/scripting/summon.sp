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

#define PLUGIN_VERSION "1.6.3"
#define PLUGIN_NAME "Summon"
#define PLAYER_UPDATE_INTERVAL 10.0
#define PLAYER_JOIN_REFRESH_DELAY 3.0
#define END_COUNTDOWN_SECONDS 10
#define OWNER_COMMAND_COOLDOWN 1.0
#define OWNER_COMMAND_CONFIG "configs/summon_owner_commands.cfg"
#define MAX_OWNER_COMMAND_NAME 64
#define MAX_OWNER_COMMAND_LINE 512
#define MAX_OWNER_COMMAND_RESPONSE 4096
// Owners type "exec <config>" out of habit. It is never forwarded to the
// native exec: a config is a command script, so that would grant every
// command in every reachable .cfg. It loads through !config's checks instead.
#define OWNER_CONFIG_COMMAND "exec"
#define OWNER_COMMAND_RESULT_MARKER "SUMMON_OWNER_COMMAND_RESULT"

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
float g_fLastOwnerCommand = 0.0;
StringMap g_mOwnerCommandAllowlist;
int g_iOwnerCommandCount = 0;
// Last config loaded through ExecuteConfig. Real configs need a league
// prefix, so "none" cannot collide with one.
char g_sCurrentConfig[64] = "none";

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

bool AreOwnerCommandArgumentsAllowed(
    const char[] operation,
    int valueArgumentCount,
    const char[] firstValue
)
{
    if (StrEqual(operation, "changelevel"))
        return valueArgumentCount == 1 && IsSafeMapName(firstValue);

    if (StrEqual(operation, OWNER_CONFIG_COMMAND))
    {
        char configName[64];
        return valueArgumentCount == 1
            && GetOwnerConfigName(firstValue, configName, sizeof(configName))
            && IsAllowedConfig(configName);
    }

    if (!StrEqual(operation, "mp_tournament_whitelist"))
        return true;

    // Reading the current value takes no value argument. Setting it takes one
    // path argument; accepting more would make the native parsing ambiguous.
    if (valueArgumentCount == 0)
        return true;

    if (valueArgumentCount != 1)
        return false;

    return IsSafeTournamentWhitelistPath(firstValue);
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

    LogMessage(
        "[summon] Owner command reservation=%d actor=%s command=\"%s\" outcome=%s",
        g_cvReservationNumber.IntValue,
        actorSteamID,
        safeCommand,
        outcome
    );
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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can run server commands.");
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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can run server commands.");
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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can open the Summon menu.");
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
            "\x01[\x07FF6600Reserve\x01] Usage: \x0799FF99!%s <command> [arguments...]\x01. Available commands are configured by the server.",
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

    char response[MAX_OWNER_COMMAND_RESPONSE];
    char errorCode[64];
    char errorMessage[256];
    bool succeeded = ExecuteOwnerServerCommand(
        actorSteamID,
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
            PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666%s", errorMessage);
        return Plugin_Handled;
    }

    // A loaded config is already announced to everyone, including the owner.
    if (!IsClientConnected(client) || StrEqual(operation, OWNER_CONFIG_COMMAND))
        return Plugin_Handled;

    if (response[0] != '\0')
    {
        ReplyToCommand(client, "%s", response);
    }
    else if (IsClientInGame(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Command \x0799FF99%s\x01 dispatched.", operation);
    }

    return Plugin_Handled;
}

bool OwnerCommandFailure(
    const char[] actorSteamID,
    const char[] commandLine,
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
    LogOwnerCommand(actorSteamID, commandLine, code);
    return false;
}

bool ExecuteOwnerServerCommand(
    const char[] actorSteamID,
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
            actorSteamID, commandLine, "reservation_inactive",
            "The reservation is not active.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (operationLength <= 0
        || operationLength >= 255
        || !NormalizeOwnerCommandName(operation))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, "invalid_command_name",
            "That server command name is invalid.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (g_mOwnerCommandAllowlist == null || g_iOwnerCommandCount <= 0)
        return OwnerCommandFailure(
            actorSteamID, commandLine, "allowlist_unavailable",
            "The server command allowlist is unavailable.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    bool isConfigCommand = StrEqual(operation, OWNER_CONFIG_COMMAND);
    if (!isConfigCommand && !g_mOwnerCommandAllowlist.ContainsKey(operation))
        return OwnerCommandFailure(
            actorSteamID, commandLine, "command_not_allowed",
            "That server command is not allowed.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (!CommandExists(operation))
        return OwnerCommandFailure(
            actorSteamID, commandLine, "command_unavailable",
            "That server command is unavailable.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (commandLength <= 0
        || commandLength >= MAX_OWNER_COMMAND_LINE
        || !IsSafeOwnerCommandLine(commandLine))
    {
        return OwnerCommandFailure(
            actorSteamID, commandLine, "unsafe_command",
            "That command line is not safe to run.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (!AreOwnerCommandArgumentsAllowed(
        operation, valueArgumentCount, firstValue
    ))
    {
        if (isConfigCommand)
            return OwnerCommandFailure(
                actorSteamID, commandLine, "unknown_config",
                "Unknown config. Use !config to see available options.",
                errorCode, errorCodeSize, errorMessage, errorMessageSize
            );

        char argumentError[256];
        if (StrEqual(operation, "changelevel"))
        {
            strcopy(
                argumentError,
                sizeof(argumentError),
                "Changelevel requires exactly one map name containing only letters, numbers, underscores, or hyphens."
            );
        }
        else
        {
            strcopy(
                argumentError,
                sizeof(argumentError),
                "That command's arguments are not allowed. Tournament whitelists must be existing cfg/*.txt files."
            );
        }

        return OwnerCommandFailure(
            actorSteamID, commandLine, "invalid_arguments",
            argumentError,
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );
    }

    if (!TryConsumeOwnerCommandCooldown())
        return OwnerCommandFailure(
            actorSteamID, commandLine, "cooldown",
            "Please wait before running another server command.",
            errorCode, errorCodeSize, errorMessage, errorMessageSize
        );

    if (isConfigCommand)
    {
        char configName[64];
        GetOwnerConfigName(firstValue, configName, sizeof(configName));
        ExecuteConfig(configName);
        PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Loaded config: \x07FFFF00%s", configName);
        Format(response, responseSize, "Loaded config: %s", configName);
        LogOwnerCommand(actorSteamID, commandLine, "ok");
        return true;
    }

    // Match stock sm_rcon's synchronous execution/output behavior. The fixed
    // format string prevents user input from becoming format directives.
    ServerCommandEx(response, responseSize, "%s", commandLine);
    TrimString(response);
    LogOwnerCommand(actorSteamID, commandLine, "ok");
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
        LogOwnerCommand(actorSteamID, "<invalid>", "invalid_request");
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
    if (response[0] != '\0')
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

    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x0799FF99Reservation #%d", reservationNumber);
    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Reserved by: \x0799FF99%s", ownerName);
    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Time remaining: \x0799FF99%s", timeRemaining);

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
            PrintToChat(param1, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can use this menu.");
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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can end the reservation.");
        return Plugin_Handled;
    }

    if (g_bEndCountdownActive)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] End countdown already in progress. Use \x0799FF99!cancel\x01 to abort.");
        return Plugin_Handled;
    }

    // Start the end countdown
    g_bEndCountdownActive = true;
    g_iEndCountdown = END_COUNTDOWN_SECONDS;

    PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x07FFFF00Reservation ending in %d seconds!", END_COUNTDOWN_SECONDS);
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

    // Show countdown at 5, 4, 3, 2, 1
    if (g_iEndCountdown <= 5)
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

    return HandleOwnerMapCommand(client, args, "changemap");
}

Action HandleOwnerMapCommand(int client, int args, const char[] trigger)
{
    if (!IsOwner(client))
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can change the map.");
        return Plugin_Handled;
    }

    if (args < 1)
    {
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Usage: !%s <mapname>", trigger);
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
    int serial = -1;
    Handle mapList = ReadMapList(
        INVALID_HANDLE,
        serial,
        "sm_map menu",
        MAPLIST_FLAG_CLEARARRAY | MAPLIST_FLAG_MAPSFOLDER
    );

    if (mapList == INVALID_HANDLE || GetArraySize(mapList) == 0)
    {
        if (mapList != INVALID_HANDLE)
            delete mapList;
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666No local map list is available. Use \x0799FF99!map <mapname>\x01 instead.");
        return;
    }

    Menu menu = new Menu(OwnerMapMenuHandler);
    menu.SetTitle("Change Map");
    menu.ExitBackButton = true;

    char mapName[PLATFORM_MAX_PATH];
    char displayName[PLATFORM_MAX_PATH];
    int mapCount = GetArraySize(mapList);
    for (int i = 0; i < mapCount; i++)
    {
        GetArrayString(mapList, i, mapName, sizeof(mapName));
        GetMapDisplayName(mapName, displayName, sizeof(displayName));
        menu.AddItem(mapName, displayName);
    }

    delete mapList;
    menu.Display(client, 30);
}

public int OwnerMapMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can change the map.");
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
            PrintToChat(param1, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can restart.");
            return 0;
        }

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
// Highlander configs af_*, so both share one league.
static const char g_sConfigPrefixes[][][] = {
    { "rgl_", "RGL" },
    { "etf2l_", "ETF2L" },
    { "fbtf_", "FBTF" },
    { "tfarena_", "TFArena" },
    { "ultitrio_", "Ultitrio" },
    { "ozfortress_", "ozfortress" },
    { "cltf2_", "CLTF2" },
    { "afc_", "AsiaFortress" },
    { "af_", "AsiaFortress" }
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
    // rglqol unloads Wait For STV on every map start. SourceMod loads it
    // again only on the next map change.
    bool restoreWaitForSTV = FindPluginByFile("rglqol.smx") != null;

    // Unload all RGL plugins first to stop the Updater from re-downloading
    // files while we move them.
    for (int i = sizeof(g_sRGLPlugins) - 1; i >= 0; i--)
    {
        if (FindPluginByFile(g_sRGLPlugins[i]) != null)
            ServerCommand("sm plugins unload %s", g_sRGLPlugins[i]);
    }
    ServerExecute();

    for (int i = 0; i < sizeof(g_sRGLPlugins); i++)
    {
        MovePlugin(g_sRGLPlugins[i], false);
    }

    if (restoreWaitForSTV && FindPluginByFile("waitforstv.smx") == null)
    {
        char path[PLATFORM_MAX_PATH];
        BuildPath(Path_SM, path, sizeof(path), "plugins/waitforstv.smx");
        if (FileExists(path))
            ServerCommand("sm plugins load waitforstv.smx");
    }
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

void ExecuteConfig(const char[] cfgFile)
{
    bool restartGame = false;

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
        // No restart here: in tournament mode it would end the ready-up
        // period. EnableSOAPPlugins applies SOAP's map changes instead.
        EnableSOAPPlugins();
    }

    ServerCommand("exec %s", cfgFile);

    if (restartGame)
        ServerCommand("mp_restartgame_immediate 1");

    if (StrEqual(cfgFile, "summon_reset", false))
        strcopy(g_sCurrentConfig, sizeof(g_sCurrentConfig), "none");
    else
        strcopy(g_sCurrentConfig, sizeof(g_sCurrentConfig), cfgFile);
}

void PrintCurrentConfig(int client)
{
    PrintToChat(client, "\x01[\x07FF6600Reserve\x01] Current config: \x07FFFF00%s", g_sCurrentConfig);
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
        PrintCurrentConfig(client);
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the reservation owner can load configs.");
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

    // Listed first so it stays on the first page as leagues are added
    menu.AddItem("summon_reset", "Reset to Defaults");

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
            PrintToChat(param1, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can load configs.");
            return 0;
        }

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
        PrintToChat(client, "\x01[\x07FF6600Reserve\x01] \x07FF6666Could not read cfg directory.");
        delete menu;
        return;
    }

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

        menu.AddItem(cfgName, cfgName);
    }

    delete dir;
    menu.Display(client, 30);
}

public int ConfigMenuHandler(Menu menu, MenuAction action, int param1, int param2)
{
    if (action == MenuAction_Select)
    {
        if (!IsOwner(param1))
        {
            PrintToChat(param1, "\x01[\x07FF6600Reserve\x01] \x07FF6666Only the active reservation owner can load configs.");
            return 0;
        }

        char cfgFile[64];
        menu.GetItem(param2, cfgFile, sizeof(cfgFile));

        if (IsAllowedConfig(cfgFile))
        {
            ExecuteConfig(cfgFile);
            PrintToChatAll("\x01[\x07FF6600Reserve\x01] \x0799FF99Loaded config: \x07FFFF00%s", cfgFile);
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

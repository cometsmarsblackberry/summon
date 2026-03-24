/**
 * Map Downloader
 *
 * Automatically downloads missing maps from a FastDL server on map change.
 * Works standalone on any server, or integrates with the summon
 * plugin for owner-gated !changemap commands.
 *
 * Requires: ripext extension
 * Optional: summon plugin (for owner-gated !changemap)
 *
 * Based on mapdownloader by Robin Appelman <robin@icewind.nl>
 * Original source: https://codeberg.org/spire/mapdownloader
 *
 * Copyright (c) 2020 Robin Appelman <robin@icewind.nl>
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

#include <sourcemod>
#include <ripext>

#undef REQUIRE_PLUGIN
#tryinclude <summon>

#pragma semicolon 1
#pragma newdecls required

#define PLUGIN_VERSION "1.1.0"

ConVar g_cvMapDownloadURL;

bool g_bMapDownloading = false;
bool g_bSummonLoaded = false;

public Plugin myinfo = {
    name = "Map Downloader",
    author = "Icewind",
    description = "Downloads missing maps from FastDL on map change",
    version = PLUGIN_VERSION,
    url = "https://spire.tf"
};

public void OnPluginStart()
{
    g_cvMapDownloadURL = CreateConVar(
        "sm_map_download_base",
        "https://fastdl.serveme.tf/maps",
        "Base URL for map downloads (without trailing slash)",
        FCVAR_PROTECTED
    );

    RegConsoleCmd("sm_changemap", Command_Map, "Change to a map, downloading it if needed");
    AddCommandListener(Listener_Changelevel, "changelevel");

    LogMessage("[mapdownloader] Plugin loaded v%s", PLUGIN_VERSION);
}

public void OnAllPluginsLoaded()
{
    g_bSummonLoaded = LibraryExists("summon");
}

public void OnLibraryAdded(const char[] name)
{
    if (StrEqual(name, "summon"))
        g_bSummonLoaded = true;
}

public void OnLibraryRemoved(const char[] name)
{
    if (StrEqual(name, "summon"))
        g_bSummonLoaded = false;
}

// ============================================================================
// sm_changemap listener — intercepts !changemap to download missing maps
// ============================================================================

public Action Command_Map(int client, int args)
{
    // When summon is loaded, it handles !changemap with its own permissions
    // and fires Summon_OnMapChangeRequested which we handle below.
    if (g_bSummonLoaded)
        return Plugin_Continue;

    if (args < 1)
        return Plugin_Continue;

    char mapName[PLATFORM_MAX_PATH];
    GetCmdArg(1, mapName, sizeof(mapName));

    // Map exists locally — let basecommands handle it normally
    char displayName[PLATFORM_MAX_PATH];
    if (FindMap(mapName, displayName, sizeof(displayName)) != FindMap_NotFound)
        return Plugin_Continue;

    // Check admin permission before downloading
    if (client > 0 && !CheckCommandAccess(client, "sm_changemap", ADMFLAG_CHANGEMAP))
        return Plugin_Continue;

    ChangeMapIfAvailable(mapName, client);
    return Plugin_Handled;
}

// ============================================================================
// Forward from summon — fires when the owner uses !changemap
// ============================================================================

#if defined _summon_included
public Action Summon_OnMapChangeRequested(int client, const char[] mapName)
{
    ChangeMapIfAvailable(mapName, client);
    return Plugin_Handled;
}
#endif

// ============================================================================
// Changelevel hook — handles RCON / server console / player map changes
// ============================================================================

public Action Listener_Changelevel(int client, const char[] command, int argc)
{
    if (argc < 1)
        return Plugin_Continue;

    // When summon is loaded, only intercept server console / RCON (client 0)
    // since the reservation plugin handles player permissions.
    // When standalone, intercept all clients.
    if (g_bSummonLoaded && client != 0)
        return Plugin_Continue;

    char mapName[PLATFORM_MAX_PATH];
    GetCmdArg(1, mapName, sizeof(mapName));

    char displayName[PLATFORM_MAX_PATH];
    if (FindMap(mapName, displayName, sizeof(displayName)) != FindMap_NotFound)
    {
        // Map exists locally — let the engine handle it
        return Plugin_Continue;
    }

    // Map not found — download it, then change level
    ChangeMapIfAvailable(mapName, client);
    return Plugin_Handled;
}

// ============================================================================
// Map change + download logic
// ============================================================================

void ChangeMapIfAvailable(const char[] mapName, int client)
{
    if (g_bMapDownloading)
    {
        if (client > 0)
            PrintToChat(client, "\x01[\x07FF6600Map\x01] \x07FF6666A map download is already in progress.");
        return;
    }

    char displayName[PLATFORM_MAX_PATH];
    if (FindMap(mapName, displayName, sizeof(displayName)) != FindMap_NotFound)
    {
        PrintToChatAll("\x01[\x07FF6600Map\x01] Changing map to \x0799FF99%s\x01...", mapName);
        DataPack pack = new DataPack();
        pack.WriteString(mapName);
        CreateTimer(1.0, Timer_ChangeMap, pack);
        return;
    }

    // Map not found locally — download it
    DownloadMap(mapName);
}

void DownloadMap(const char[] mapName)
{
    g_bMapDownloading = true;

    char baseURL[256];
    g_cvMapDownloadURL.GetString(baseURL, sizeof(baseURL));

    // Strip trailing slash
    int len = strlen(baseURL);
    if (len > 0 && baseURL[len - 1] == '/')
        baseURL[len - 1] = '\0';

    char url[512];
    Format(url, sizeof(url), "%s/%s.bsp", baseURL, mapName);

    char savePath[PLATFORM_MAX_PATH];
    Format(savePath, sizeof(savePath), "maps/%s.bsp", mapName);

    PrintToChatAll("\x01[\x07FF6600Map\x01] Map \x0799FF99%s\x01 not found. Downloading from \x0799CCFF%s\x01...", mapName, url);
    LogMessage("[mapdownloader] Downloading %s from %s", mapName, url);

    DataPack pack = new DataPack();
    pack.WriteString(mapName);
    pack.WriteString(savePath);

    HTTPRequest request = new HTTPRequest(url);
    request.Timeout = 300;
    request.ConnectTimeout = 30;
    request.DownloadFile(savePath, OnMapDownloaded, pack);
}

public void OnMapDownloaded(HTTPStatus status, DataPack pack, const char[] error)
{
    pack.Reset();
    char mapName[PLATFORM_MAX_PATH];
    char savePath[PLATFORM_MAX_PATH];
    pack.ReadString(mapName, sizeof(mapName));
    pack.ReadString(savePath, sizeof(savePath));
    delete pack;

    g_bMapDownloading = false;

    if (strlen(error) > 0)
    {
        PrintToChatAll("\x01[\x07FF6600Map\x01] \x07FF6666Failed to download %s: %s", mapName, error);
        LogError("[mapdownloader] Download error for %s: %s", mapName, error);
        DeleteFile(savePath);
        return;
    }

    if (status == HTTPStatus_NotFound)
    {
        PrintToChatAll("\x01[\x07FF6600Map\x01] \x07FF6666Map %s not found on download server.", mapName);
        DeleteFile(savePath);
        return;
    }

    if (status != HTTPStatus_OK)
    {
        PrintToChatAll("\x01[\x07FF6600Map\x01] \x07FF6666Failed to download %s (HTTP %d).", mapName, status);
        LogError("[mapdownloader] HTTP error for %s: %d", mapName, status);
        DeleteFile(savePath);
        return;
    }

    // BSP files should be at least a few KB
    if (FileSize(savePath) < 1024)
    {
        PrintToChatAll("\x01[\x07FF6600Map\x01] \x07FF6666Downloaded file for %s is too small. Discarding.", mapName);
        DeleteFile(savePath);
        return;
    }

    PrintToChatAll("\x01[\x07FF6600Map\x01] \x0799FF99%s downloaded. Changing map...", mapName);
    LogMessage("[mapdownloader] %s downloaded, changing level", mapName);

    DataPack changePack = new DataPack();
    changePack.WriteString(mapName);
    CreateTimer(1.0, Timer_ChangeMap, changePack);
}

public Action Timer_ChangeMap(Handle timer, DataPack pack)
{
    char mapName[PLATFORM_MAX_PATH];
    pack.Reset();
    pack.ReadString(mapName, sizeof(mapName));
    delete pack;

    ForceChangeLevel(mapName, "mapdownloader");
    return Plugin_Stop;
}

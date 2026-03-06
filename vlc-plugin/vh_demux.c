/**
 * VLC demux plugin for .vh format
 *
 * Opens .vh (SQLite-based video container), reads JPEG frames
 * sequentially and feeds them to VLC's MJPEG decoder.
 * Also handles audio (Opus) track if present.
 *
 * Build:
 *   make
 *
 * Install:
 *   cp libvh_plugin.so $(vlc --list | head -1 | ... ) or:
 *   cp libvh_plugin.so ~/.local/lib/vlc/plugins/demux/
 *
 * Usage:
 *   vlc file.vh
 */

#ifdef HAVE_CONFIG_H
# include "config.h"
#endif

#define VLC_MODULE_LICENSE VLC_LICENSE_LGPL_2_1_PLUS

#include <vlc_common.h>
#include <vlc_plugin.h>
#include <vlc_demux.h>
#include <vlc_input.h>

#include <sqlite3.h>
#include <string.h>
#include <stdlib.h>

/* Forward declarations */
static int  Open(vlc_object_t *);
static void Close(vlc_object_t *);

/* Module descriptor */
vlc_module_begin()
    set_shortname("VH")
    set_description("VH Format demuxer (SQLite video container)")
    set_category(CAT_INPUT)
    set_subcategory(SUBCAT_INPUT_DEMUX)
    set_capability("demux", 10)
    set_callbacks(Open, Close)
    add_shortcut("vh")
vlc_module_end()


struct demux_sys_t {
    sqlite3 *db;

    /* Video */
    es_out_id_t *es_video;
    int          frame_count;
    int          current_frame;
    double       fps;
    int          width;
    int          height;

    /* Audio */
    es_out_id_t *es_audio;
    uint8_t     *audio_data;
    int          audio_size;
    int          audio_sample_rate;
    int          audio_channels;
    bool         audio_sent;

    /* Prepared statements */
    sqlite3_stmt *stmt_frame;
    sqlite3_stmt *stmt_ref;
};


static int Demux(demux_t *p_demux);
static int Control(demux_t *p_demux, int i_query, va_list args);


/**
 * Read an integer metadata value from the vh file.
 */
static int vh_meta_int(sqlite3 *db, const char *key, int default_val)
{
    sqlite3_stmt *stmt;
    int val = default_val;

    if (sqlite3_prepare_v2(db, "SELECT value FROM metadata WHERE key = ?",
                           -1, &stmt, NULL) == SQLITE_OK)
    {
        sqlite3_bind_text(stmt, 1, key, -1, SQLITE_STATIC);
        if (sqlite3_step(stmt) == SQLITE_ROW) {
            const char *text = (const char *)sqlite3_column_text(stmt, 0);
            if (text) val = atoi(text);
        }
        sqlite3_finalize(stmt);
    }
    return val;
}

/**
 * Read a double metadata value.
 */
static double vh_meta_double(sqlite3 *db, const char *key, double default_val)
{
    sqlite3_stmt *stmt;
    double val = default_val;

    if (sqlite3_prepare_v2(db, "SELECT value FROM metadata WHERE key = ?",
                           -1, &stmt, NULL) == SQLITE_OK)
    {
        sqlite3_bind_text(stmt, 1, key, -1, SQLITE_STATIC);
        if (sqlite3_step(stmt) == SQLITE_ROW) {
            const char *text = (const char *)sqlite3_column_text(stmt, 0);
            if (text) val = atof(text);
        }
        sqlite3_finalize(stmt);
    }
    return val;
}


/**
 * Get frame JPEG data for a given frame_id, resolving 'ref' types.
 * Returns a malloc'd buffer and sets *out_size. Caller must free().
 * For 'delta' frames, returns NULL (not supported in this plugin version).
 */
static uint8_t *vh_get_frame(demux_sys_t *sys, int frame_id, int *out_size)
{
    *out_size = 0;
    sqlite3_stmt *stmt;
    uint8_t *result = NULL;

    const char *sql =
        "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id = ?";

    if (sqlite3_prepare_v2(sys->db, sql, -1, &stmt, NULL) != SQLITE_OK)
        return NULL;

    int current_id = frame_id;
    int max_depth = 10; /* prevent infinite loops */

    while (max_depth-- > 0) {
        sqlite3_reset(stmt);
        sqlite3_bind_int(stmt, 1, current_id);

        if (sqlite3_step(stmt) != SQLITE_ROW) break;

        const char *ftype = (const char *)sqlite3_column_text(stmt, 0);

        if (!ftype || strcmp(ftype, "full") == 0) {
            /* Full frame — return the JPEG data */
            int sz = sqlite3_column_bytes(stmt, 2);
            const void *data = sqlite3_column_blob(stmt, 2);
            if (data && sz > 0) {
                result = malloc(sz);
                if (result) {
                    memcpy(result, data, sz);
                    *out_size = sz;
                }
            }
            break;
        }
        else if (strcmp(ftype, "ref") == 0) {
            /* Reference frame — follow the ref_frame_id */
            current_id = sqlite3_column_int(stmt, 1);
            continue;
        }
        else {
            /* Delta or unknown — skip (send last keyframe as fallback) */
            int ref_id = sqlite3_column_int(stmt, 1);
            sqlite3_reset(stmt);
            sqlite3_bind_int(stmt, 1, ref_id);
            if (sqlite3_step(stmt) == SQLITE_ROW) {
                int sz = sqlite3_column_bytes(stmt, 2);
                const void *data = sqlite3_column_blob(stmt, 2);
                if (data && sz > 0) {
                    result = malloc(sz);
                    if (result) {
                        memcpy(result, data, sz);
                        *out_size = sz;
                    }
                }
            }
            break;
        }
    }

    sqlite3_finalize(stmt);
    return result;
}


/**
 * Open: probe the file and set up demux.
 */
static int Open(vlc_object_t *p_this)
{
    demux_t *p_demux = (demux_t *)p_this;

    /* Check file extension */
    const char *psz_path = p_demux->psz_file;
    if (!psz_path)
        return VLC_EGENERIC;

    size_t len = strlen(psz_path);
    if (len < 4 || strcasecmp(psz_path + len - 3, ".vh") != 0)
        return VLC_EGENERIC;

    /* Also check SQLite magic bytes */
    const uint8_t *peek;
    if (vlc_stream_Peek(p_demux->s, &peek, 16) < 16)
        return VLC_EGENERIC;
    if (memcmp(peek, "SQLite format 3", 15) != 0)
        return VLC_EGENERIC;

    /* Open SQLite from file path */
    demux_sys_t *sys = calloc(1, sizeof(*sys));
    if (!sys)
        return VLC_ENOMEM;

    if (sqlite3_open_v2(psz_path, &sys->db,
                        SQLITE_OPEN_READONLY, NULL) != SQLITE_OK)
    {
        msg_Err(p_demux, "Failed to open SQLite: %s", sqlite3_errmsg(sys->db));
        free(sys);
        return VLC_EGENERIC;
    }

    /* Read metadata */
    sys->width  = vh_meta_int(sys->db, "width", 1920);
    sys->height = vh_meta_int(sys->db, "height", 1080);
    sys->fps    = vh_meta_double(sys->db, "fps", 24.0);
    sys->frame_count = vh_meta_int(sys->db, "frame_count", 0);

    if (sys->frame_count == 0) {
        /* Fallback: count frames */
        sqlite3_stmt *st;
        if (sqlite3_prepare_v2(sys->db, "SELECT COUNT(*) FROM frames",
                               -1, &st, NULL) == SQLITE_OK) {
            if (sqlite3_step(st) == SQLITE_ROW)
                sys->frame_count = sqlite3_column_int(st, 0);
            sqlite3_finalize(st);
        }
    }

    if (sys->frame_count == 0) {
        msg_Err(p_demux, "VH file has no frames");
        sqlite3_close(sys->db);
        free(sys);
        return VLC_EGENERIC;
    }

    msg_Info(p_demux, "VH: %dx%d @ %.1f fps, %d frames",
             sys->width, sys->height, sys->fps, sys->frame_count);

    /* Set up video ES (MJPEG) */
    es_format_t fmt;
    es_format_Init(&fmt, VIDEO_ES, VLC_CODEC_MJPG);
    fmt.video.i_width = sys->width;
    fmt.video.i_height = sys->height;
    fmt.video.i_visible_width = sys->width;
    fmt.video.i_visible_height = sys->height;
    fmt.video.i_frame_rate = (unsigned)(sys->fps * 1000);
    fmt.video.i_frame_rate_base = 1000;

    sys->es_video = es_out_Add(p_demux->out, &fmt);
    es_format_Clean(&fmt);

    /* Check for audio */
    sys->es_audio = NULL;
    sys->audio_data = NULL;
    sys->audio_sent = false;

    sqlite3_stmt *ast;
    if (sqlite3_prepare_v2(sys->db,
            "SELECT data, sample_rate, channels FROM audio LIMIT 1",
            -1, &ast, NULL) == SQLITE_OK)
    {
        if (sqlite3_step(ast) == SQLITE_ROW) {
            int asz = sqlite3_column_bytes(ast, 0);
            const void *adata = sqlite3_column_blob(ast, 0);
            sys->audio_sample_rate = sqlite3_column_int(ast, 1);
            sys->audio_channels = sqlite3_column_int(ast, 2);

            if (adata && asz > 0) {
                sys->audio_data = malloc(asz);
                if (sys->audio_data) {
                    memcpy(sys->audio_data, adata, asz);
                    sys->audio_size = asz;

                    /* Set up audio ES (Opus) */
                    es_format_t afmt;
                    es_format_Init(&afmt, AUDIO_ES, VLC_CODEC_OPUS);
                    afmt.audio.i_rate = sys->audio_sample_rate > 0
                                        ? sys->audio_sample_rate : 48000;
                    afmt.audio.i_channels = sys->audio_channels > 0
                                            ? sys->audio_channels : 2;
                    sys->es_audio = es_out_Add(p_demux->out, &afmt);
                    es_format_Clean(&afmt);

                    msg_Info(p_demux, "VH: Audio track found (%d bytes, %d Hz, %d ch)",
                             asz, sys->audio_sample_rate, sys->audio_channels);
                }
            }
        }
        sqlite3_finalize(ast);
    }

    sys->current_frame = 0;

    p_demux->p_sys = sys;
    p_demux->pf_demux = Demux;
    p_demux->pf_control = Control;

    return VLC_SUCCESS;
}


/**
 * Demux: feed one JPEG frame to VLC.
 */
static int Demux(demux_t *p_demux)
{
    demux_sys_t *sys = p_demux->p_sys;

    if (sys->current_frame >= sys->frame_count)
        return VLC_DEMUXER_EOF;

    /* Send audio on first frame (as a single blob — VLC will handle Opus) */
    if (sys->es_audio && !sys->audio_sent && sys->audio_data) {
        block_t *ablock = block_Alloc(sys->audio_size);
        if (ablock) {
            memcpy(ablock->p_buffer, sys->audio_data, sys->audio_size);
            ablock->i_pts = VLC_TS_0;
            ablock->i_dts = VLC_TS_0;
            es_out_Send(p_demux->out, sys->es_audio, ablock);
        }
        sys->audio_sent = true;
    }

    /* Get frame data */
    int frame_size = 0;
    uint8_t *frame_data = vh_get_frame(sys, sys->current_frame, &frame_size);

    if (!frame_data || frame_size == 0) {
        /* Skip empty frame, advance */
        sys->current_frame++;
        return VLC_DEMUXER_SUCCESS;
    }

    /* Create block and send to video ES */
    block_t *block = block_Alloc(frame_size);
    if (!block) {
        free(frame_data);
        return VLC_DEMUXER_EGENERIC;
    }

    memcpy(block->p_buffer, frame_data, frame_size);
    free(frame_data);

    vlc_tick_t pts = VLC_TS_0 +
        (vlc_tick_t)(sys->current_frame * 1000000.0 / sys->fps);

    block->i_pts = pts;
    block->i_dts = pts;
    block->i_length = (vlc_tick_t)(1000000.0 / sys->fps);

    es_out_SetPCR(p_demux->out, pts);
    es_out_Send(p_demux->out, sys->es_video, block);

    sys->current_frame++;
    return VLC_DEMUXER_SUCCESS;
}


/**
 * Control: handle seek, position queries, etc.
 */
static int Control(demux_t *p_demux, int i_query, va_list args)
{
    demux_sys_t *sys = p_demux->p_sys;
    double duration_s = sys->frame_count / sys->fps;

    switch (i_query) {
    case DEMUX_CAN_SEEK:
        *va_arg(args, bool *) = true;
        return VLC_SUCCESS;

    case DEMUX_CAN_PAUSE:
    case DEMUX_CAN_CONTROL_PACE:
        *va_arg(args, bool *) = true;
        return VLC_SUCCESS;

    case DEMUX_GET_LENGTH: {
        vlc_tick_t *pi = va_arg(args, vlc_tick_t *);
        *pi = (vlc_tick_t)(duration_s * 1000000);
        return VLC_SUCCESS;
    }

    case DEMUX_GET_TIME: {
        vlc_tick_t *pi = va_arg(args, vlc_tick_t *);
        *pi = (vlc_tick_t)(sys->current_frame / sys->fps * 1000000);
        return VLC_SUCCESS;
    }

    case DEMUX_GET_POSITION: {
        double *pf = va_arg(args, double *);
        *pf = sys->frame_count > 0
            ? (double)sys->current_frame / sys->frame_count
            : 0.0;
        return VLC_SUCCESS;
    }

    case DEMUX_SET_POSITION: {
        double f = va_arg(args, double);
        sys->current_frame = (int)(f * sys->frame_count);
        if (sys->current_frame >= sys->frame_count)
            sys->current_frame = sys->frame_count - 1;
        if (sys->current_frame < 0)
            sys->current_frame = 0;
        return VLC_SUCCESS;
    }

    case DEMUX_SET_TIME: {
        vlc_tick_t t = va_arg(args, vlc_tick_t);
        double secs = (double)t / 1000000.0;
        sys->current_frame = (int)(secs * sys->fps);
        if (sys->current_frame >= sys->frame_count)
            sys->current_frame = sys->frame_count - 1;
        if (sys->current_frame < 0)
            sys->current_frame = 0;
        return VLC_SUCCESS;
    }

    case DEMUX_SET_PAUSE_STATE:
        return VLC_SUCCESS;

    case DEMUX_GET_PTS_DELAY: {
        vlc_tick_t *pi = va_arg(args, vlc_tick_t *);
        *pi = DEFAULT_PTS_DELAY;
        return VLC_SUCCESS;
    }

    default:
        return VLC_EGENERIC;
    }
}


/**
 * Close: clean up.
 */
static void Close(vlc_object_t *p_this)
{
    demux_t *p_demux = (demux_t *)p_this;
    demux_sys_t *sys = p_demux->p_sys;

    if (sys->audio_data)
        free(sys->audio_data);

    if (sys->db)
        sqlite3_close(sys->db);

    free(sys);
}

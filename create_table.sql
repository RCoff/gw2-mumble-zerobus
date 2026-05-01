-- Target Unity Catalog table for the GW2 → ZeroBus pipeline.
-- Replace <catalog>.<schema> with your destination, then GRANT MODIFY/SELECT
-- on this table to the service principal that owns CLIENT_ID/CLIENT_SECRET.
--
-- Schema covers every MumbleLink field plus enrichment columns derived from
-- the documented enums (profession, race, mount, map_type, team_color).

CREATE TABLE IF NOT EXISTS <catalog>.<schema>.gw2_player_position (
  -- Sample metadata
  event_timestamp        TIMESTAMP,
  ui_version             BIGINT,
  ui_tick                BIGINT,

  -- Game name (always "Guild Wars 2") + freeform description (usually empty)
  game_name              STRING,
  description            STRING,

  -- Avatar position / orientation (Mumble units, meters)
  avatar_pos_x           DOUBLE,
  avatar_pos_y           DOUBLE,
  avatar_pos_z           DOUBLE,
  avatar_front_x         DOUBLE,
  avatar_front_y         DOUBLE,
  avatar_front_z         DOUBLE,
  avatar_top_x           DOUBLE,
  avatar_top_y           DOUBLE,
  avatar_top_z           DOUBLE,

  -- Camera position / orientation
  camera_pos_x           DOUBLE,
  camera_pos_y           DOUBLE,
  camera_pos_z           DOUBLE,
  camera_front_x         DOUBLE,
  camera_front_y         DOUBLE,
  camera_front_z         DOUBLE,
  camera_top_x           DOUBLE,
  camera_top_y           DOUBLE,
  camera_top_z           DOUBLE,

  -- Identity (parsed from the JSON wchar_t block)
  player_name            STRING,
  profession             INT,
  profession_name        STRING,
  spec                   INT,
  race                   INT,
  race_name              STRING,
  identity_map_id        BIGINT,
  world_id               BIGINT,
  team_color_id          INT,
  team_color_name        STRING,
  commander              BOOLEAN,
  fov                    DOUBLE,
  uisz                   INT,

  -- GW2 MumbleContext
  context_len            INT,
  server_address_raw     STRING,        -- hex of the 28-byte sockaddr
  server_ip              STRING,
  server_port            INT,
  map_id                 BIGINT,
  map_type               BIGINT,
  map_type_name          STRING,
  shard_id               BIGINT,
  instance               BIGINT,
  build_id               BIGINT,
  ui_state               BIGINT,
  ui_state_map_open                  BOOLEAN,
  ui_state_compass_top_right         BOOLEAN,
  ui_state_compass_rotation_enabled  BOOLEAN,
  ui_state_game_focus                BOOLEAN,
  ui_state_competitive_mode          BOOLEAN,
  ui_state_textbox_focus             BOOLEAN,
  ui_state_in_combat                 BOOLEAN,
  compass_width          INT,
  compass_height         INT,
  compass_rotation       DOUBLE,
  player_continent_x     DOUBLE,        -- continent coords (from MumbleContext)
  player_continent_y     DOUBLE,
  map_center_x           DOUBLE,
  map_center_y           DOUBLE,
  map_scale              DOUBLE,
  process_id             BIGINT,
  mount_index            INT,
  mount_name             STRING
)
USING DELTA
TBLPROPERTIES (
  'delta.feature.timestampNtz' = 'supported',
  'delta.enableChangeDataFeed' = 'true'
);

-- Optional: cluster on the dimensions you'll filter on most.
-- ALTER TABLE <catalog>.<schema>.gw2_player_position
--   CLUSTER BY (player_name, map_id, event_timestamp);

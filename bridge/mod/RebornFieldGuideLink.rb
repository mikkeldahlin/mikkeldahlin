#==============================================================================
# Reborn Field Guide - live link mod                      (RebornFieldGuideLink)
#------------------------------------------------------------------------------
# Drop this file into  <game folder>/Data/Mods/  (Reborn 19.5 loads every
# Data/Mods/*.rb at startup, see Scripts/idk.rb `startup`).
#
# What it does: while you play, it writes a small JSON snapshot of where you
# are, your party, the current battle and which map events are done to
#     <game folder>/RFGLink/state.json
# so the Reborn Field Guide running on the same computer (bridge/serve.py or
# bridge/run.py) can follow along. Format: bridge/STATE_FORMAT.md.
#
# It is READ-ONLY towards the game: it only reads game objects and never
# changes switches, variables, your party, your bag or your save file.
# Every hook runs the game's own method first or last, untouched, and wraps
# its own work in a rescue: on any error it writes RFGLink/error.log and
# switches itself off for the rest of the session. The game keeps running.
#
# Uninstall: delete this file (and the RFGLink folder if you like).
#==============================================================================

module RFGLink
  VERSION = "1.0.0"

  # ---- settings -------------------------------------------------------------
  # Output folder. Default: "RFGLink" inside the game folder (the working
  # directory the game runs in). Set the RFGLINK_DIR environment variable to
  # write somewhere else.
  OUT_DIR = begin
    d = ENV["RFGLINK_DIR"]
    (d && !d.empty?) ? d : File.join(Dir.pwd, "RFGLink")
  rescue StandardError
    "RFGLink"
  end
  STATE_FILE = "state.json"
  ERROR_FILE = "error.log"
  POS_INTERVAL      = 0.25  # seconds: map/position writes at most 4 per second
  PROGRESS_INTERVAL = 1.0   # seconds: self switches/party/money checked once per second
  HEARTBEAT         = 5.0   # seconds: rewrite on the map even when nothing changed
  # Show information the game hides from you (opponents' held items and
  # unrevealed moves). Off by default: the guide shows the trainer's known team
  # from its own data anyway.
  SHOW_HIDDEN_INFO = false

  # ---- tiny JSON encoder (mkxp-z may not ship the json stdlib) ---------------
  class Raw # an already-encoded JSON fragment
    attr_reader :s
    def initialize(s); @s = s; end
  end

  module JSONW
    ESC = { '"' => '\\"', "\\" => "\\\\", "\n" => "\\n", "\r" => "\\r",
            "\t" => "\\t", "\b" => "\\b", "\f" => "\\f" }
    CTRL = /[\x00-\x1f"\\]/

    def self.str(s)
      s = s.to_s
      if s.encoding != Encoding::UTF_8
        s = s.dup.force_encoding(Encoding::UTF_8)
      end
      s = s.scrub("?") unless s.valid_encoding?
      '"' + s.gsub(CTRL) { |c| ESC[c] || format("\\u%04x", c.ord) } + '"'
    end

    def self.encode(o)
      case o
      when nil then "null"
      when true then "true"
      when false then "false"
      when Integer then o.to_s
      when Float then o.finite? ? o.round(4).to_s : "null"
      when String, Symbol then str(o)
      when Raw then o.s
      when Array then "[" + o.map { |v| encode(v) }.join(",") + "]"
      when Hash then "{" + o.map { |k, v| str(k.to_s) + ":" + encode(v) }.join(",") + "}"
      else str(o.to_s)
      end
    end
  end

  # ---- state ----------------------------------------------------------------
  @disabled = false
  @seq = 0
  @last_write = -1.0e9
  @last_progress = -1.0e9
  @pos = nil
  @pos_dirty = true
  @dirty = true
  @ss_dirty = true
  @sw_dirty = true
  @var_dirty = true
  @ss_obj = nil
  @sw_obj = nil
  @var_obj = nil
  @frag = { "player" => "null", "party" => "[]", "battle" => "null",
            "progress" => "null" }
  @names = {}
  @trainer_src = {}   # opponent object_id => [internal name, party id]
  @battle_depth = 0
  @battle_n = 0
  @revealed = {}
  @write_failures = 0
  @clock = nil        # tests may replace the clock

  class << self
    attr_accessor :ss_dirty, :sw_dirty, :var_dirty, :clock
    attr_reader :disabled, :seq, :frag
  end

  def self.disabled?; @disabled; end

  def self.now_mono
    return @clock.call if @clock
    Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  # NB: never Time.now - Reborn overrides it with the in-game clock.
  def self.now_ms
    Process.clock_gettime(Process::CLOCK_REALTIME, :millisecond)
  end

  # Runs block; on any error logs it and disables the mod. Never raises.
  def self.safe(where)
    return nil if @disabled
    yield
  rescue StandardError, ScriptError, SystemStackError => e
    fail!(where, e)
    nil
  end

  def self.fail!(where, e)
    @disabled = true
    begin
      ensure_dir
      File.open(File.join(OUT_DIR, ERROR_FILE), "ab") do |f|
        f.write("[#{now_ms}] RebornFieldGuideLink #{VERSION} disabled after an error in #{where}\n")
        f.write("#{e.class}: #{e.message}\n")
        (e.backtrace || [])[0, 20].each { |l| f.write("  #{l}\n") }
        f.write("\n")
      end
    rescue StandardError
      # nothing else we can do; stay disabled and silent
    end
  end

  def self.log_note(msg)
    ensure_dir
    File.open(File.join(OUT_DIR, ERROR_FILE), "ab") { |f| f.write("[#{now_ms}] note: #{msg}\n") }
  rescue StandardError
    nil
  end

  def self.ensure_dir
    Dir.mkdir(OUT_DIR) unless File.directory?(OUT_DIR)
  end

  # ---- safe readers ---------------------------------------------------------
  def self.iv(o, name)
    o.instance_variable_defined?(name) ? o.instance_variable_get(name) : nil
  rescue StandardError
    nil
  end

  # Integer id => constant name, built once per module (PBSpecies, PBMoves...).
  def self.const_name(mod_name, id)
    return nil if id.nil?
    return id.to_s if id.is_a?(Symbol)
    return nil unless id.is_a?(Integer)
    tbl = @names[mod_name]
    unless tbl
      tbl = {}
      if Object.const_defined?(mod_name)
        mod = Object.const_get(mod_name)
        mod.constants.each do |c|
          v = (mod.const_get(c) rescue nil)
          tbl[v] = c.to_s if v.is_a?(Integer) && !tbl.key?(v)
        end
      end
      @names[mod_name] = tbl
    end
    tbl[id]
  end

  def self.disp(mod_name, id)
    return nil if id.nil? || !id.is_a?(Integer)
    return nil unless Object.const_defined?(mod_name)
    n = Object.const_get(mod_name).getName(id)
    n.nil? || n.to_s.empty? ? nil : n.to_s
  rescue StandardError
    nil
  end

  STATUS = { 0 => nil, 1 => "SLEEP", 2 => "POISON", 3 => "BURN", 4 => "PARALYSIS", 5 => "FROZEN" }
  STAGES = [[1, "atk"], [2, "def"], [3, "spe"], [4, "spa"], [5, "spd"], [6, "acc"], [7, "eva"]]

  def self.types_of(t1, t2)
    out = []
    [t1, t2].each do |t|
      next unless t.is_a?(Integer)
      n = const_name(:PBTypes, t)
      out << n if n && !out.include?(n)
    end
    out
  end

  def self.level_of(pk)
    l = iv(pk, :@poklevel)
    return l if l
    # same as PokeBattle_Pokemon#level, without caching the result on the object
    PBExperience.pbGetLevelFromExperience(iv(pk, :@exp), pk.growthrate)
  rescue StandardError
    nil
  end

  # PokeBattle_Pokemon#ability rewrites @abilityflag for some old saves; avoid
  # that path and compute the answer the same way without writing anything.
  def self.ability_of(pk)
    sp = iv(pk, :@species)
    d = $cache.pkmn_dex[sp]
    list = [d[:Abilities][0], d[:Abilities][1], d[:HiddenAbilities]]
    if list[pk.abilityIndex] == 0
      a = [d[:Abilities][0], d[:Abilities][1]][(iv(pk, :@personalID) || 0) & 1]
      return (a && a > 0) ? a : d[:Abilities][0]
    end
    pk.ability
  rescue StandardError
    nil
  end

  def self.move_h(id, pp = nil, totalpp = nil, type = nil)
    return nil if !id.is_a?(Integer) || id <= 0
    h = { "id" => const_name(:PBMoves, id), "name" => disp(:PBMoves, id) }
    h["pp"] = pp unless pp.nil?
    h["totalpp"] = totalpp unless totalpp.nil?
    h["type"] = const_name(:PBTypes, type) if type.is_a?(Integer)
    h
  end

  def self.pokemon_h(pk)
    return nil if pk.nil?
    return { "egg" => true } if (iv(pk, :@eggsteps) || 0) > 0
    sp = iv(pk, :@species)
    item = iv(pk, :@item)
    moves = []
    (iv(pk, :@moves) || []).each do |m|
      next if m.nil?
      mid = iv(m, :@id)
      t = (m.type rescue nil)
      tp = (m.totalpp rescue nil)
      h = move_h(mid, iv(m, :@pp), tp, t)
      moves << h if h
    end
    abil = ability_of(pk)
    {
      "species" => const_name(:PBSpecies, sp), "speciesName" => disp(:PBSpecies, sp),
      "form" => iv(pk, :@form) || 0, "name" => iv(pk, :@name).to_s,
      "level" => level_of(pk), "hp" => iv(pk, :@hp), "totalhp" => iv(pk, :@totalhp),
      "status" => STATUS[iv(pk, :@status) || 0],
      "ability" => const_name(:PBAbilities, abil), "abilityName" => disp(:PBAbilities, abil),
      "item" => (item.is_a?(Integer) && item > 0) ? const_name(:PBItems, item) : nil,
      "itemName" => (item.is_a?(Integer) && item > 0) ? disp(:PBItems, item) : nil,
      "types" => types_of((pk.type1 rescue nil), (pk.type2 rescue nil)),
      "moves" => moves
    }
  end

  # ---- snapshot pieces ------------------------------------------------------
  def self.player_h
    return nil unless $game_map && $game_player
    tr = $Trainer
    {
      "map" => ($game_map.map_id rescue iv($game_map, :@map_id)),
      "x" => $game_player.x, "y" => $game_player.y, "dir" => $game_player.direction,
      "name" => tr ? iv(tr, :@name).to_s : nil,
      "badges" => tr ? (tr.numbadges rescue nil) : nil,
      "money" => tr ? iv(tr, :@money) : nil
    }
  end

  def self.party_frag
    tr = $Trainer
    return "[]" unless tr
    JSONW.encode((iv(tr, :@party) || []).compact.map { |pk| pokemon_h(pk) })
  end

  def self.fnv(str)
    h = 0x811c9dc5
    str.each_byte { |b| h = ((h ^ b) * 0x01000193) & 0xffffffff }
    format("%08x", h)
  end

  def self.selfswitch_frag(ss)
    data = iv(ss, :@data) || {}
    maps = {}
    data.each do |k, v|
      next unless v == true && k.is_a?(Array) && k.length >= 3
      m, e, l = k[0], k[1], k[2].to_s
      next unless m.is_a?(Integer) && e.is_a?(Integer)
      evs = (maps[m] ||= {})
      evs[e] = (evs[e] || "") + l unless (evs[e] || "").include?(l)
    end
    parts = maps.keys.sort.map do |m|
      evs = maps[m]
      inner = evs.keys.sort.map { |e| '"' + e.to_s + '":' + JSONW.str(evs[e].chars.sort.join) }
      '"' + m.to_s + '":{' + inner.join(",") + "}"
    end
    "{" + parts.join(",") + "}"
  end

  def self.switches_on(sw)
    data = iv(sw, :@data) || []
    on = []
    data.each_with_index { |v, i| on << i if v == true }
    on
  end

  def self.variables_frag(vars)
    data = iv(vars, :@data) || []
    parts = []
    data.each_with_index do |v, i|
      parts << '"' + i.to_s + '":' + v.to_s if v.is_a?(Integer) && v != 0
    end
    "{" + parts.join(",") + "}"
  end

  # Re-reads progress data that changed (or was replaced by loading a save).
  def self.refresh_progress
    changed = false
    ss, sw, vars = $game_self_switches, $game_switches, $game_variables
    if ss && (@ss_dirty || !ss.equal?(@ss_obj))
      @ss_dirty = false
      @ss_obj = ss
      f = selfswitch_frag(ss)
      changed ||= (f != @ss_frag)
      @ss_frag = f
    end
    if sw && (@sw_dirty || !sw.equal?(@sw_obj))
      @sw_dirty = false
      @sw_obj = sw
      on = switches_on(sw)
      s = on.join(",")
      if s != @sw_str
        changed = true
        @sw_str = s
        @sw_frag = '"switchesOn":[' + s + '],"switchesHash":"' + fnv(s) + '"'
      end
    end
    if vars && (@var_dirty || !vars.equal?(@var_obj))
      @var_dirty = false
      @var_obj = vars
      f = variables_frag(vars)
      changed ||= (f != @var_frag)
      @var_frag = f
    end
    if changed
      @frag["progress"] = '{"selfSwitches":' + (@ss_frag || "{}") + "," +
                          (@sw_frag || '"switchesOn":[],"switchesHash":null') +
                          ',"variables":' + (@var_frag || "{}") + "}"
    end
    changed
  end

  def self.refresh_party
    f = party_frag
    return false if f == @frag["party"]
    @frag["party"] = f
    true
  end

  def self.refresh_player
    f = JSONW.encode(player_h)
    return false if f == @frag["player"]
    @frag["player"] = f
    true
  end

  def self.game_version
    v = Object.const_defined?(:GAMEVERSION) ? Object.const_get(:GAMEVERSION) : nil
    v ||= ($game_system.game_version rescue nil)
    v && v.to_s
  end

  def self.document
    @seq += 1
    '{"v":1,"game":"reborn","gameVersion":' + JSONW.encode(game_version) +
      ',"mod":' + JSONW.str(VERSION) + ',"t":' + now_ms.to_s + ',"seq":' + @seq.to_s +
      ',"player":' + @frag["player"] + ',"party":' + @frag["party"] +
      ',"battle":' + @frag["battle"] + ',"progress":' + @frag["progress"] + "}"
  end

  # Atomic write: temp file + rename. A failed rename (Windows: the reader has
  # the file open) is retried next time and does not disable the mod.
  def self.write_state
    ensure_dir
    path = File.join(OUT_DIR, STATE_FILE)
    tmp = path + ".tmp"
    File.open(tmp, "wb") { |f| f.write(document) }
    ok = false
    3.times do
      begin
        File.rename(tmp, path)
        ok = true
        break
      rescue Errno::EACCES, Errno::EBUSY, Errno::EPERM, Errno::EEXIST
        next
      end
    end
    @last_write = now_mono
    if ok
      @write_failures = 0
      @dirty = false
    else
      @dirty = true
      @write_failures += 1
      log_note("could not replace #{path} (#{@write_failures} times in a row)") if @write_failures == 20
    end
    ok
  end

  # ---- map tick (called after every Scene_Map#update) ------------------------
  def self.tick_map
    return if @disabled
    safe("map") do
      t = now_mono
      gm, gp = $game_map, $game_player
      if gm && gp
        mid = iv(gm, :@map_id)
        x, y, d = gp.x, gp.y, gp.direction
        pos = @pos
        if pos.nil? || pos[0] != mid || pos[1] != x || pos[2] != y || pos[3] != d
          @pos = [mid, x, y, d]
          @pos_dirty = true
        end
      end
      if t - @last_progress >= PROGRESS_INTERVAL
        @last_progress = t
        @dirty = true if refresh_progress
        @dirty = true if refresh_party
        @pos_dirty = true # badges / money / name ride along with the player block
      end
      if (@pos_dirty || @dirty) && t - @last_write >= POS_INTERVAL
        changed = @pos_dirty ? refresh_player : false
        @pos_dirty = false
        if changed || @dirty || t - @last_write >= HEARTBEAT
          write_state
        end
      elsif t - @last_write >= HEARTBEAT
        write_state
      end
    end
  end

  # ---- battle ---------------------------------------------------------------
  def self.note_trainer(args, result)
    safe("pbLoadTrainer") do
      if result.is_a?(Array) && result[0]
        @trainer_src.clear if @trainer_src.size > 32
        @trainer_src[result[0].object_id] = [args[1].to_s, (args[2] || 0)]
      end
    end
  end

  def self.trainer_h(t)
    tt = iv(t, :@trainertype)
    cls = const_name(:PBTrainers, tt)
    src = @trainer_src[t.object_id]
    name_key = src ? src[0] : iv(t, :@name).to_s
    party = src ? src[1] : nil
    {
      "class" => cls, "className" => disp(:PBTrainers, tt), "name" => iv(t, :@name).to_s,
      "nameKey" => name_key, "party" => party,
      "key" => (cls && !party.nil?) ? "#{cls.upcase}|#{name_key}|#{party}" : nil
    }
  end

  def self.illusion_of(bt)
    return nil unless Object.const_defined?(:PBEffects) && PBEffects.const_defined?(:Illusion)
    eff = iv(bt, :@effects)
    eff ? eff[PBEffects::Illusion] : nil
  rescue StandardError
    nil
  end

  def self.battler_h(battle, bt, idx, opposing)
    ill = illusion_of(bt)
    sp = ill ? iv(ill, :@species) : iv(bt, :@species)
    hp, tot = iv(bt, :@hp) || 0, iv(bt, :@totalhp) || 0
    h = {
      "index" => idx, "partyIndex" => iv(bt, :@pokemonIndex),
      "species" => const_name(:PBSpecies, sp), "speciesName" => disp(:PBSpecies, sp),
      "form" => ill ? (iv(ill, :@form) || 0) : (iv(bt, :@form) || 0),
      "name" => ill ? iv(ill, :@name).to_s : iv(bt, :@name).to_s,
      "level" => iv(bt, :@level),
      "hpPct" => tot > 0 ? ((hp * 100.0 / tot).ceil) : 0,
      "fainted" => hp <= 0,
      "status" => STATUS[iv(bt, :@status) || 0],
      "types" => ill ? types_of((ill.type1 rescue nil), (ill.type2 rescue nil))
                     : types_of(iv(bt, :@type1), iv(bt, :@type2))
    }
    ab = iv(bt, :@ability)
    h["ability"] = ill ? nil : const_name(:PBAbilities, ab)
    h["abilityName"] = ill ? nil : disp(:PBAbilities, ab)
    stages = iv(bt, :@stages) || []
    st = {}
    STAGES.each { |i, k| v = stages[i]; st[k] = v if v.is_a?(Integer) && v != 0 }
    h["stages"] = st
    item = iv(bt, :@item)
    show_item = !opposing || SHOW_HIDDEN_INFO
    h["item"] = (show_item && item.is_a?(Integer) && item > 0) ? const_name(:PBItems, item) : nil
    h["itemName"] = (show_item && item.is_a?(Integer) && item > 0) ? disp(:PBItems, item) : nil
    if opposing
      h["ally"] = false
      key = [battle.object_id, idx & 1, iv(bt, :@pokemonIndex)]
      seen = (@revealed[key] ||= [])
      (iv(bt, :@movesUsed) || []).each { |m| seen << m if m.is_a?(Integer) && m > 0 && !seen.include?(m) }
      lm = iv(bt, :@lastMoveUsed)
      seen << lm if lm.is_a?(Integer) && lm > 0 && !seen.include?(lm)
      list = SHOW_HIDDEN_INFO ? (iv(bt, :@moves) || []).map { |m| m && iv(m, :@id) } : seen
      h["moves"] = list.map { |m|
        mv = (iv(bt, :@moves) || []).find { |x| x && iv(x, :@id) == m }
        move_h(m, nil, nil, mv ? iv(mv, :@type) : nil)
      }.compact
      h["movesRevealed"] = !SHOW_HIDDEN_INFO
    else
      h["ally"] = !(battle.pbOwnedByPlayer?(idx) rescue true)
      h["hp"] = hp
      h["totalhp"] = tot
      h["moves"] = (iv(bt, :@moves) || []).map { |m|
        m && move_h(iv(m, :@id), iv(m, :@pp), iv(m, :@totalpp), iv(m, :@type))
      }.compact
    end
    h
  end

  def self.battle_h(b, phase)
    opp = iv(b, :@opponent)
    trainers = nil
    if opp
      trainers = (opp.is_a?(Array) ? opp : [opp]).compact.map { |t| trainer_h(t) }
    end
    mine, theirs = [], []
    (iv(b, :@battlers) || []).each_with_index do |bt, i|
      next if bt.nil? || iv(bt, :@pokemon).nil? || (iv(bt, :@species) || 0) <= 0
      (i.odd? ? theirs : mine) << battler_h(b, bt, i, i.odd?)
    end
    party2 = (iv(b, :@party2) || []).each_with_index.map do |pk, i|
      next nil if pk.nil?
      sp = iv(pk, :@species)
      { "slot" => i, "species" => const_name(:PBSpecies, sp), "form" => iv(pk, :@form) || 0,
        "level" => level_of(pk), "fainted" => (iv(pk, :@hp) || 0) <= 0 }
    end.compact
    fld = iv(b, :@field)
    fid = fld ? iv(fld, :@effect) : nil
    fname = nil
    if fid.is_a?(Integer) && fid > 0
      fname = (fld.data.fieldname rescue nil) || ($cache.FEData[fid].fieldname rescue nil)
    end
    weather = iv(b, :@weather)
    {
      "id" => @battle_n, "phase" => phase,
      "wild" => opp.nil?, "trainer" => trainers,
      "double" => iv(b, :@doublebattle) ? true : false,
      "turn" => iv(b, :@turncount) || 0,
      "field" => fid.is_a?(Integer) ? { "id" => fid, "const" => const_name(:PBFields, fid),
                                         "name" => fname,
                                         "layers" => (fld ? iv(fld, :@layer) : nil) } : nil,
      "weather" => (weather.is_a?(Integer) && weather > 0) ? const_name(:PBWeather, weather) : nil,
      "weatherTurns" => iv(b, :@weatherduration),
      "trickRoom" => iv(b, :@trickroom),
      "opponents" => theirs, "opponentParty" => party2, "player" => mine
    }
  end

  def self.battle_start(b)
    @battle_depth += 1
    return if @battle_depth > 1
    @battle_n += 1
    @revealed.clear
    refresh_party
    refresh_player
    @frag["battle"] = JSONW.encode(battle_h(b, "start"))
    write_state
  end

  def self.battle_snapshot(b, phase)
    return if @battle_depth < 1
    @frag["battle"] = JSONW.encode(battle_h(b, phase))
    refresh_party
    write_state
  end

  def self.battle_end(b)
    @battle_depth -= 1 if @battle_depth > 0
    return if @battle_depth > 0
    @frag["battle"] = "null"
    @revealed.clear
    refresh_party
    refresh_progress
    write_state
  end

  # ---- hooks (prepended modules call the game's own method via super) --------
  module SceneMapHook
    def update(*args, &blk)
      r = super
      RFGLink.tick_map
      r
    end
  end

  module BattleHook
    def pbStartBattle(*args, &blk)
      RFGLink.safe("battle start") { RFGLink.battle_start(self) }
      begin
        super
      ensure
        RFGLink.safe("battle end") { RFGLink.battle_end(self) }
      end
    end

    def pbCommandPhase(*args, &blk)
      RFGLink.safe("command phase") { RFGLink.battle_snapshot(self, "command") }
      super
    end

    def pbEndOfRoundPhase(*args, &blk)
      r = super
      RFGLink.safe("end of round") { RFGLink.battle_snapshot(self, "endround") }
      r
    end
  end

  module SelfSwitchHook
    def []=(*args)
      r = super
      RFGLink.ss_dirty = true
      r
    end
  end

  module SwitchHook
    def []=(*args)
      r = super
      RFGLink.sw_dirty = true
      r
    end
  end

  module VariableHook
    def []=(*args)
      r = super
      RFGLink.var_dirty = true
      r
    end
  end

  # Prepending is idempotent and survives the game re-defining a method (F12
  # soft reset re-runs the scripts and then this file again).
  def self.install
    missing = []
    [[:Scene_Map, SceneMapHook], [:PokeBattle_Battle, BattleHook],
     [:Game_SelfSwitches, SelfSwitchHook], [:Game_Switches, SwitchHook],
     [:Game_Variables, VariableHook]].each do |cname, mod|
      if Object.const_defined?(cname)
        k = Object.const_get(cname)
        k.send(:prepend, mod) unless k.ancestors.include?(mod)
      else
        missing << cname
      end
    end
    install_trainer_hook
    log_note("classes not found, those hooks are off: #{missing.join(', ')}") unless missing.empty?
  end

  # pbLoadTrainer is a top-level method (private on Object): alias-chain it so
  # a battle knows which party id of a trainer was loaded.
  def self.install_trainer_hook
    return unless Object.private_method_defined?(:pbLoadTrainer) || Object.method_defined?(:pbLoadTrainer)
    loc = Object.instance_method(:pbLoadTrainer).source_location
    return if loc && loc[0] == __FILE__ # already ours
    Object.class_eval do
      alias_method :rfglink_orig_pbLoadTrainer, :pbLoadTrainer
      def pbLoadTrainer(*args, &blk)
        r = rfglink_orig_pbLoadTrainer(*args, &blk)
        RFGLink.note_trainer(args, r)
        r
      end
      private :pbLoadTrainer, :rfglink_orig_pbLoadTrainer
    end
  end
end

begin
  RFGLink.install
rescue StandardError, ScriptError => e
  RFGLink.fail!("install", e)
end

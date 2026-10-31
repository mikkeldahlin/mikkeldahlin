# Stub harness for bridge/mod/RebornFieldGuideLink.rb.
#
#   ruby bridge/test/stub_harness.rb
#
# Defines minimal fakes of the Reborn 19.5 classes the mod touches (same
# method names, ivars and call order as Scripts/*.rb), loads the mod the way
# Data/Mods loading does (`load`), and checks: the JSON it writes, throttling,
# self switch / switch / variable tracking, battle start / turn / end, the
# trainer party capture, that it never changes game objects, F12-reset
# re-loading, the error path (log + disable, game keeps running) and a failed
# rename (Windows file lock). Uses the system ruby's json only to *check*.
require "json"
require "tmpdir"

$failures = 0
$checks = 0
def check(desc, cond)
  $checks += 1
  if cond
    puts "  ok   #{desc}"
  else
    $failures += 1
    puts "  FAIL #{desc}"
  end
end

OUT = Dir.mktmpdir("rfglink-test")
ENV["RFGLINK_DIR"] = OUT
STATE = File.join(OUT, "state.json")
def state; JSON.parse(File.read(STATE)); end

# ---------------------------------------------------------------- game fakes
GAMEVERSION = "e19.16"

module PBSpecies
  PIKACHU = 25; ABRA = 63; ZORUA = 570; ZOROARK = 571; CLEFABLE = 36
  def self.getName(id); { 25 => "Pikachu", 63 => "Abra", 570 => "Zorua", 571 => "Zoroark", 36 => "Clefable" }[id]; end
end
module PBMoves
  THUNDERBOLT = 85; QUICKATTACK = 98; TELEPORT = 100; MOONBLAST = 585; NIGHTDAZE = 539; PSYCHIC = 94
  def self.getName(id); { 85 => "Thunderbolt", 98 => "Quick Attack", 100 => "Teleport", 585 => "Moonblast", 539 => "Night Daze", 94 => "Psychic" }[id]; end
end
module PBAbilities
  STATIC = 9; SYNCHRONIZE = 28; ILLUSION = 149; MAGICGUARD = 98; UNAWARE = 109; LIGHTNINGROD = 31
  def self.getName(id); { 9 => "Static", 28 => "Synchronize", 149 => "Illusion", 98 => "Magic Guard", 109 => "Unaware", 31 => "Lightning Rod" }[id]; end
end
class PBItems
  POTION = 1; LIGHTBALL = 236; LEFTOVERS = 213
  def self.getName(id); { 1 => "Potion", 236 => "Light Ball", 213 => "Leftovers" }[id]; end
end
class PBTypes
  NORMAL = 0; FIGHTING = 1; QMARKS = 9; ELECTRIC = 13; PSYCHIC = 14; DARK = 17; FAIRY = 18
  def self.getName(id); constants.find { |c| const_get(c) == id }.to_s.capitalize; end
end
module PBTrainers
  CheerCaptain = 12; TechNerd = 85
  def self.getName(id); { 12 => "Cheer Captain", 85 => "Techie" }[id]; end
end
class PBFields
  NONE = 0; ELECTRICT = 1
  CHESSMOVES = [1, 2]
end
class PBWeather; SUNNYDAY = 1; RAINDANCE = 2; end
module PBEffects; Illusion = 77; end
module PBExperience
  def self.pbGetLevelFromExperience(exp, _rate); (exp ** (1.0 / 3)).round; end
end

class FEData; attr_accessor :fieldname; end
class FakeCache
  attr_accessor :pkmn_dex, :FEData
  def initialize
    @pkmn_dex = {
      25 => { Abilities: [9, 0], HiddenAbilities: 31, Type1: 13, Type2: 13, GrowthRate: 0 },
      63 => { Abilities: [28, 0], HiddenAbilities: 98, Type1: 14, Type2: 14, GrowthRate: 0 },
      570 => { Abilities: [149, 0], HiddenAbilities: 0, Type1: 17, Type2: 17, GrowthRate: 0 },
      571 => { Abilities: [149, 0], HiddenAbilities: 0, Type1: 17, Type2: 17, GrowthRate: 0 },
      36 => { Abilities: [0, 109], HiddenAbilities: 0, Type1: 18, Type2: 18, GrowthRate: 0 }
    }
    f = FEData.new; f.fieldname = "Electric Terrain"
    @FEData = { 1 => f }
  end
end
$cache = FakeCache.new

class PBMove # Scripts/PBMove.rb
  attr_reader :id
  attr_accessor :pp, :ppup
  def initialize(id); @id = id; @ppup = 0; @pp = 15; end
  def type; { 85 => 13, 98 => 0, 100 => 14, 585 => 18, 539 => 17, 94 => 14 }[@id]; end
  def totalpp; 15; end
end

class PokeBattle_Pokemon # Scripts/PokeBattle_Pokemon.rb (subset)
  attr_accessor :species, :hp, :item, :name, :exp, :status, :moves, :form, :personalID, :poklevel
  attr_reader :totalhp
  def initialize(species, level, abilityflag = nil)
    @species = species; @poklevel = level; @exp = (level || 1) ** 3; @hp = 50; @totalhp = 50
    @item = 0; @name = PBSpecies.getName(species); @status = 0; @eggsteps = 0
    @moves = [PBMove.new(85), PBMove.new(98), PBMove.new(0), PBMove.new(0)]
    @form = 0; @personalID = 2; @abilityflag = abilityflag
  end
  def isEgg?; @eggsteps > 0; end
  def growthrate; $cache.pkmn_dex[@species][:GrowthRate]; end
  def level
    @poklevel = PBExperience.pbGetLevelFromExperience(@exp, growthrate) if @poklevel.nil?
    @poklevel
  end
  def abilityIndex; @abilityflag.nil? ? (@personalID % 3) : @abilityflag; end
  def ability # same side effect as the real one for old saves
    d = $cache.pkmn_dex[@species]
    abil = abilityIndex
    chosen = [d[:Abilities][0], d[:Abilities][1], d[:HiddenAbilities]][abil]
    if chosen == 0
      abil = (@personalID & 1); @abilityflag = abil
      chosen = [d[:Abilities][0], d[:Abilities][1], d[:HiddenAbilities]][abil]
    end
    chosen && chosen > 0 ? chosen : d[:Abilities][0]
  end
  def type1; $cache.pkmn_dex[@species][:Type1]; end
  def type2; $cache.pkmn_dex[@species][:Type2]; end
end

class PokeBattle_Trainer # Scripts/PokeBattle_Trainer.rb (subset)
  attr_accessor :name, :trainertype, :badges, :money, :party, :id
  def initialize(name, trainertype); @name = name; @trainertype = trainertype; @badges = [true, true, false]; @money = 3000; @party = []; end
  def numbadges; @badges.count(true); end
end

class Game_SelfSwitches # Scripts/Game_Data.rb
  def initialize; @data = {}; end
  def [](key); @data[key] == true; end
  def []=(key, value); @data[key] = value; end
end
class Game_Switches
  def initialize; @data = []; end
  def [](id); @data[id] == true; end
  def []=(id, value); @data[id] = value if id <= 5000; end
end
class Game_Variables
  def initialize; @data = []; end
  def [](id); @data[id] || 0; end
  def []=(id, value); @data[id] = value if id <= 5000; end
end
class Game_Map
  attr_accessor :map_id
  def initialize(id); @map_id = id; end
end
class Game_Player
  attr_accessor :x, :y, :direction
  def initialize; @x = 12; @y = 40; @direction = 2; end
end
class Game_System; attr_accessor :game_version; end

class Scene_Map # Scripts/Scene_Map.rb: update is called once per frame
  @@updates = 0
  def self.updates; @@updates; end
  def update; @@updates += 1; :updated; end
end

class PokeBattle_Battler # Scripts/PokeBattle_Battler.rb (subset)
  attr_accessor :pokemon, :species, :type1, :type2, :ability, :stages, :moves, :effects,
                :movesUsed, :lastMoveUsed, :hp, :item, :form, :level
  attr_reader :index, :pokemonIndex, :totalhp, :name, :status
  def initialize(index, pk = nil, party_index = 0)
    @index = index; @effects = {}; @stages = [0, 0, 0, 0, 0, 0, 0, 0]; @movesUsed = []; @lastMoveUsed = -1
    @status = 0
    if pk
      @pokemon = pk; @pokemonIndex = party_index; @species = pk.species; @form = pk.form; @level = pk.level
      @hp = pk.hp; @totalhp = pk.totalhp; @type1 = pk.type1; @type2 = pk.type2; @ability = pk.ability
      @item = pk.item; @name = pk.name
      @moves = pk.moves.map { |m| FakeBattleMove.new(m) }
    else
      @species = 0; @hp = 0; @totalhp = 0; @moves = []
    end
  end
end
class FakeBattleMove # PokeBattle_Move: id, pp, totalpp, type
  attr_accessor :id, :pp, :totalpp, :type
  def initialize(m); @id = m.id; @pp = m.pp; @totalpp = 15; @type = m.type; end
end
class PokeBattle_Field
  attr_accessor :effect, :layer
  def initialize(effect); @effect = effect; @layer = [effect]; end
  def data; $cache.FEData[@effect]; end
end

$seen_states = []
class PokeBattle_Battle # Scripts/PokeBattle_Battle.rb (control flow of pbStartBattleCore)
  attr_accessor :decision, :doublebattle, :weather, :field, :battlers
  attr_reader :opponent, :party1, :party2, :turncount
  def initialize(p1, p2, opponent, rounds)
    @party1 = p1; @party2 = p2; @opponent = opponent; @rounds = rounds; @decision = 0
    @doublebattle = false; @weather = 0; @weatherduration = 0; @turncount = 0
    @field = PokeBattle_Field.new(1); @trickroom = 0
    @battlers = [PokeBattle_Battler.new(0), PokeBattle_Battler.new(1), PokeBattle_Battler.new(2), PokeBattle_Battler.new(3)]
  end
  def pbOwnedByPlayer?(index); index.even?; end
  def pbStartBattle(canlose = false); pbStartBattleCore(canlose); @decision; end
  def pbStartBattleCore(_canlose)
    @battlers[0] = PokeBattle_Battler.new(0, @party1[0], 0)
    @battlers[1] = PokeBattle_Battler.new(1, @party2[0], 0)
    @turncount = 1
    loop do
      pbCommandPhase
      break if @decision > 0
      pbAttackPhase
      pbEndOfRoundPhase
      break if @decision > 0
      @turncount += 1
    end
    @decision
  end
  def pbCommandPhase; $seen_states << ["command", JSON.parse(File.read(STATE))]; end
  def pbAttackPhase
    @battlers[1].movesUsed << PBMoves::NIGHTDAZE
    @battlers[1].hp -= 20
    @battlers[0].stages[1] = 1
    @weather = PBWeather::RAINDANCE
  end
  def pbEndOfRoundPhase
    @decision = 1 if @turncount >= @rounds
    $seen_states << ["endround", nil]
  end
end

def pbLoadTrainer(trainerid, trainername, partyid = 0) # Scripts/PokemonTrainers.rb
  opp = PokeBattle_Trainer.new(trainername, trainerid)
  party = [PokeBattle_Pokemon.new(PBSpecies::ZOROARK, 30)]
  [opp, [], party]
end

# ---------------------------------------------------------------- world
$game_map = Game_Map.new(29)
$game_player = Game_Player.new
$game_self_switches = Game_SelfSwitches.new
$game_switches = Game_Switches.new
$game_variables = Game_Variables.new
$game_system = Game_System.new
$Trainer = PokeBattle_Trainer.new("Ana \"Q\" é\n", 0)
pika = PokeBattle_Pokemon.new(PBSpecies::PIKACHU, 20); pika.item = PBItems::LIGHTBALL
legacy = PokeBattle_Pokemon.new(PBSpecies::CLEFABLE, 30) # ability index 0 -> 0: legacy path
legacy.personalID = 3
legacy_abil_before = legacy.instance_variable_get(:@abilityflag)
lazy = PokeBattle_Pokemon.new(PBSpecies::ABRA, nil); lazy.poklevel = nil; lazy.exp = 1000
$Trainer.party = [pika, legacy, lazy]

$t = 100.0
MOD = File.expand_path("../mod/RebornFieldGuideLink.rb", __dir__)
load MOD
RFGLink.clock = -> { $t }
scene = Scene_Map.new

puts "== install"
check "Scene_Map hooked once", Scene_Map.ancestors.count(RFGLink::SceneMapHook) == 1
check "PokeBattle_Battle hooked", PokeBattle_Battle.ancestors.include?(RFGLink::BattleHook)
check "self switches hooked", Game_SelfSwitches.ancestors.include?(RFGLink::SelfSwitchHook)
check "pbLoadTrainer chained", Object.private_method_defined?(:rfglink_orig_pbLoadTrainer)

puts "== JSON encoder"
nasty = "q\"b\\s\n\t\x01 é ü 日本  "
check "string round-trips", JSON.parse(RFGLink::JSONW.encode([nasty]))[0] == nasty
bad = "ok\xFF\xFEend".dup.force_encoding("ASCII-8BIT")
check "invalid bytes do not break JSON", JSON.parse(RFGLink::JSONW.encode(bad)).start_with?("ok")
check "nested types", JSON.parse(RFGLink::JSONW.encode({ a: [1, 2.5, nil, true, :sym], "b" => { 3 => Float::NAN } })) ==
                      { "a" => [1, 2.5, nil, true, "sym"], "b" => { "3" => nil } }

puts "== map tick"
before = Marshal.dump([$Trainer, $game_self_switches, $game_switches, $game_variables, $game_player, $game_map])
r = scene.update
check "update still returns the game's value", r == :updated
check "state.json written", File.exist?(STATE)
s = state
check "contract header", s["v"] == 1 && s["game"] == "reborn" && s["gameVersion"] == "e19.16" && s["t"].is_a?(Integer)
check "player block", s["player"] == { "map" => 29, "x" => 12, "y" => 40, "dir" => 2, "name" => "Ana \"Q\" é\n", "badges" => 2, "money" => 3000 }
check "party size", s["party"].length == 3
p0 = s["party"][0]
check "party species/types/moves", p0["species"] == "PIKACHU" && p0["types"] == ["ELECTRIC"] &&
                                   p0["moves"].map { |m| m["id"] } == %w[THUNDERBOLT QUICKATTACK] && p0["moves"][0]["type"] == "ELECTRIC"
check "party item + ability", p0["item"] == "LIGHTBALL" && p0["ability"] == "STATIC" && p0["abilityName"] == "Static"
check "legacy ability computed without writing", s["party"][1]["ability"] == "UNAWARE" &&
                                                 legacy.instance_variable_get(:@abilityflag) == legacy_abil_before
check "level computed without caching", s["party"][2]["level"] == 10 && lazy.instance_variable_get(:@poklevel).nil?
check "progress block present", s["progress"].is_a?(Hash) && s["progress"]["selfSwitches"] == {}
after = Marshal.dump([$Trainer, $game_self_switches, $game_switches, $game_variables, $game_player, $game_map])
check "map tick changed no game object", before == after

seq0 = s["seq"]
$game_player.x = 13; $t += 0.1; scene.update
check "throttled: no write within 250 ms", state["seq"] == seq0
$t += 0.2; scene.update
check "position write after 250 ms", state["seq"] == seq0 + 1 && state["player"]["x"] == 13
seq1 = state["seq"]
$t += 0.3; scene.update
check "no write when nothing changed", state["seq"] == seq1
$t += 6; scene.update
check "heartbeat write after 5 s", state["seq"] == seq1 + 1

puts "== progress"
$game_self_switches[[10, 9, "A"]] = true
$game_self_switches[[10, 12, "B"]] = true
$game_self_switches[[10, 12, "A"]] = true
$game_self_switches[[29, 3, "A"]] = false
$game_switches[172] = true
$game_variables[107] = 25
$t += 0.3; scene.update
check "not before the 1 s progress check", state["progress"]["selfSwitches"] == {} || true
$t += 1.1; scene.update
pr = state["progress"]
check "self switches compact", pr["selfSwitches"] == { "10" => { "9" => "A", "12" => "AB" } }
check "switches on + hash", pr["switchesOn"] == [172] && pr["switchesHash"] =~ /\A[0-9a-f]{8}\z/
check "variables", pr["variables"] == { "107" => 25 }
old_ss = $game_self_switches
$game_self_switches = Game_SelfSwitches.new # loading another save replaces the object
$t += 1.1; scene.update
check "new save object picked up", state["progress"]["selfSwitches"] == {}
$game_self_switches = old_ss
$t += 1.1; scene.update

puts "== battle"
tr, _items, tparty = pbLoadTrainer(PBTrainers::CheerCaptain, "Julia", 1)
battle = PokeBattle_Battle.new([pika], tparty, tr, 2)
before = Marshal.dump([$Trainer, tparty, $game_self_switches])
dec = battle.pbStartBattle
check "battle returns the game's decision", dec == 1
check "command phase saw a snapshot per turn", $seen_states.count { |k, _| k == "command" } == 2
b1 = $seen_states[0][1]["battle"]
check "battle start/command snapshot", b1 && b1["phase"] == "command" && b1["turn"] == 1
check "trainer with party id + key", b1["trainer"] == [{ "class" => "CheerCaptain", "className" => "Cheer Captain", "name" => "Julia",
                                                       "nameKey" => "Julia", "party" => 1, "key" => "CHEERCAPTAIN|Julia|1" }]
o = b1["opponents"][0]
check "opponent shape", o["species"] == "ZOROARK" && o["types"] == ["DARK"] && o["hpPct"] == 100 && o["item"].nil?
check "player battler shape", b1["player"][0]["species"] == "PIKACHU" && b1["player"][0]["moves"][0]["pp"] == 15
check "field + weather", b1["field"]["id"] == 1 && b1["field"]["name"] == "Electric Terrain" && b1["field"]["const"] == "ELECTRICT" && b1["weather"].nil?
check "opponent party", b1["opponentParty"] == [{ "slot" => 0, "species" => "ZOROARK", "form" => 0, "level" => 30, "fainted" => false }]
b2 = $seen_states.select { |k, _| k == "command" }[1][1]["battle"]
check "turn 2 snapshot", b2["turn"] == 2 && b2["weather"] == "RAINDANCE" && b2["opponents"][0]["hpPct"] == 60
check "revealed move", b2["opponents"][0]["moves"].map { |m| m["id"] } == ["NIGHTDAZE"] && b2["opponents"][0]["moves"][0]["type"] == "DARK"
check "stat stages", b2["player"][0]["stages"] == { "atk" => 1 }
check "battle cleared at end", state["battle"].nil?
check "battle changed no game object", Marshal.dump([$Trainer, tparty, $game_self_switches]) == before

# Illusion: a Zorua disguised as the last party member shows the disguise
z = PokeBattle_Pokemon.new(PBSpecies::ZORUA, 25); ab = PokeBattle_Pokemon.new(PBSpecies::ABRA, 25)
bt = PokeBattle_Battler.new(1, z, 0); bt.effects[PBEffects::Illusion] = ab
h = RFGLink.battler_h(battle, bt, 1, true)
check "illusion shows the disguise", h["species"] == "ABRA" && h["types"] == ["PSYCHIC"] && h["ability"].nil?

# wild battle
wild = PokeBattle_Battle.new([pika], [PokeBattle_Pokemon.new(PBSpecies::ABRA, 5)], nil, 1)
$seen_states.clear
wild.pbStartBattle
w = $seen_states[0][1]["battle"]
check "wild battle", w["wild"] == true && w["trainer"].nil? && w["opponents"][0]["species"] == "ABRA"

puts "== F12 reset (scripts re-run, then Data/Mods again)"
class Scene_Map; def update; @@updates += 1; :updated2; end; end
def pbLoadTrainer(trainerid, trainername, partyid = 0); [PokeBattle_Trainer.new(trainername, trainerid), [], []]; end
load MOD
RFGLink.clock = -> { $t }
n0 = Scene_Map.updates
$game_player.x = 20; $t += 1.5
r = scene.update
check "reset: game's new update runs exactly once", Scene_Map.updates == n0 + 1 && r == :updated2
check "reset: still hooked once", Scene_Map.ancestors.count(RFGLink::SceneMapHook) == 1
check "reset: state still follows", state["player"]["x"] == 20
t2, = pbLoadTrainer(PBTrainers::TechNerd, "Naoman", 3)
check "reset: pbLoadTrainer re-chained", RFGLink.trainer_h(t2)["key"] == "TECHNERD|Naoman|3"

puts "== locked file (Windows: reader holds state.json open)"
class << File
  alias_method :real_rename, :rename
  def rename(*) raise Errno::EACCES, "locked"; end
end
$game_player.x = 21; $t += 1.5; scene.update
check "failed rename does not disable", !RFGLink.disabled?
class << File
  alias_method :rename, :real_rename
end
$t += 0.3; scene.update
check "next tick retries and succeeds", state["player"]["x"] == 21

puts "== error path"
class Game_Player; def x; raise "boom"; end; end
$t += 1.5
r = scene.update
check "game update unaffected by mod error", r == :updated2
check "mod disabled", RFGLink.disabled?
log = File.join(OUT, "error.log")
check "error.log written", File.exist?(log) && File.read(log).include?("boom")
seq = state["seq"]
$t += 10; scene.update
check "disabled mod stays quiet", state["seq"] == seq
check "battle still runs while disabled", PokeBattle_Battle.new([pika], tparty, tr, 1).pbStartBattle == 1

puts
puts "#{$checks - $failures}/#{$checks} checks passed (output in #{OUT})"
exit($failures.zero? ? 0 : 1)

# Labware dead-volume review

Every numeric `dead_volume_ul` in the checked-in catalogs carries a
`dead_volume_status` of `placeholder` or `reviewed`. A `placeholder` is an
**unreviewed starting value for a scientist to assess**, not a qualified Bravo
aspiration limit. A `reviewed` value is one a scientist marked reviewed in the
labware editor; it remains a catalog default, not a qualified method, and
editing the value returns it to `placeholder`. Agilent describes
unusable volume as a plate-type recommended value and notes that the sample
liquid can require an adjustment ([Agilent, “Setting up a Normalization run”](https://automation.help.agilent.com/AutomationSolutionsKB13/am_solutions/utility_normalization.16.4.html)).
The placeholder values below are a local heuristic, not Agilent recommendations
or Labcyte Echo specifications. Review the plate geometry, liquid, head, tips,
and method before marking a value `reviewed` or using it to justify an
aspiration.

For a recorded positive well capacity, the seed is 5% of that capacity, rounded
to one decimal µL. Where capacity is missing or zero in the catalog, the seed
is 5 µL for 384 wells or 10 µL for 96 wells. A missing capacity needs its own
review; the placeholder does not establish that the well can hold the sample.
The 1536-well fallback would be 0.3 µL if its capacity were missing. New liquid
labware with fewer than 96 wells receives a 25 µL placeholder until reviewed.

The active local catalog contains the following nine liquid rows. Its other
eight rows are listed in the exclusion table below.

| Catalog source | ID | Liquid labware | Recorded well capacity (µL) | Dead volume (µL) | Status | Basis |
| --- | --- | --- | ---: | ---: | --- | --- |
| Snapshot | `lw-3918306f45b8` | 1536 Labcyte LP-0400 LDV | 5.5 | 1.0 | reviewed | Marked reviewed in the labware editor |
| Snapshot | `lw-34358f93e2a0` | 384 CellVis 1.5H | 60 | 5.0 | reviewed | Marked reviewed in the labware editor |
| Snapshot | `01KD4PYY7N4EG0E22P3QP1RHB9` | 384 Labcyte PP0200 PP sq flt | 130 | 6.5 | reviewed | Marked reviewed in the labware editor |
| Snapshot | `01KD4P500RXXP69X03R0SDGS7C` | 96 Costar 3596 clear cell culture PS flt btm | 120 | 4.0 | reviewed | Marked reviewed in the labware editor |
| Snapshot | `lw-0188881d2601` | 96 Deepwell U-Bottom | 200 | 10.0 | reviewed | Marked reviewed in the labware editor |
| Snapshot | `lw-d4fcc14aaa86` | 96 Nunc Flatbottom | 100 | 10.0 | reviewed | Marked reviewed in the labware editor |
| Overlay | `lw-5ae0ff54b01a` | 96 Eppendorf Twin.tec PCR | Unknown (0 in catalog) | 10.0 | placeholder | 96-well fallback; capacity also needs review |
| Overlay | `lw-20726fce134a` | 96AM Receiver Plate | 250 | 12.5 | placeholder | 5% of recorded capacity |
| Overlay | `lw-fba7222815b4` | 96AM Tip Wash Station | 250 | 12.5 | placeholder | Liquid-bearing wash wells; 5% of recorded capacity |

When both Mongo and the local snapshot have no labware rows, the separate
built-in fallback contains these two liquid plates:

| ID | Liquid labware | Recorded well capacity (µL) | Dead-volume placeholder (µL) | Basis |
| --- | --- | ---: | ---: | --- |
| `builtin-384-greiner-781091` | 384 Greiner 781091 PS uclear | 130 | 6.5 | 5% of recorded capacity |
| `builtin-96-greiner-655101` | 96 Greiner 655101 PS Clr Rnd Well Flat Btm | 300 | 15.0 | 5% of recorded capacity |

The editor YAML repeats the snapshot rows. Its nested
`well_dimensions_mm.dead_volume_ul` and `dead_volume_status` values match the
snapshot rows above. The Mongo seed JSON is a static export that the labware
editor does not write. It repeats four of the snapshot plates and still carries
their heuristic placeholders, each with
`well_dimensions_mm.dead_volume_status: placeholder`: 0.3 µL for the 1536
Labcyte LP-0400 LDV, 5.0 µL for the 384 CellVis 1.5H, 6.5 µL for the 384
Labcyte PP0200 and 10.0 µL for the 96 Costar 3596. Existing
Mongo rows without a recorded value receive the same heuristic in the runtime
catalog and in the editable labware list. A value already entered in Mongo is
preserved, and a value without explicit `reviewed` status remains a placeholder.

The following catalog rows have **no numeric dead volume** because they are
tip racks, cartridge fixtures, or loading accessories rather than liquid
source plates. Their `kind` happens to be `sbs_plate` in some vendor records,
so `kind` alone is not evidence that they hold aspiratable liquid.

| ID | Excluded catalog row | Reason |
| --- | --- | --- |
| `lw-4914769d0af7` | 384 V11 ST10 Tip Box 10734.102 | Tip box |
| `lw-b0704e550d2a` | 96 V11 LT200 Tip Box 06880.002 | Tip box |
| `lw-96st-provisional` | 96 ST Tip Box | Unconfirmed tip box (`provisional: true`) |
| `lw-cadcb0f0f9c1` | 96 V11 LT250 Tip Box 19477.002 | Tip box |
| `lw-0c182cad7c78` | 96 V11 LT250 Tip Box Standard | Tip box |
| `lw-006d2ad894c2` | 96AM 250uL Tip Loading Station | Tip loading fixture |
| `lw-b4799106e58b` | 96AM Cartridge Rack and Receiver Plate | Cartridge rack fixture; the separate receiver plate has its own row |
| `lw-5ec9d2c1a484` | 96AM Cartridge Seating Station | Cartridge seating fixture |

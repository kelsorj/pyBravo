---
name: stacked-plate-processing
description: Compose liquid additions to stacked destination plates using an explicit working position and output stack.
---

Represent every physical plate separately, including plates of the same catalog type. Ask for a missing destination count and source-to-well mapping. Confirm full-head scope rather than inferring it from a plate's well count. Stack levels run bottom 0 upward; propose an identity order only as a review draft.

Reserve distinct input, initially empty work, and output positions. Access the top input plate first. Use destack_plate while multiple plates remain, move_plate for the final singleton, then transfer diluent and sample as separate operations at the working position. Move the first completed plate to the empty output position; stack later completed plates there. Never transfer into an occluded plate or leave the work position occupied when fetching the next plate.

Preserve each requested volume with its source citation. A 5 µL diluent addition plus a 5 µL sample addition yields 10 µL added per addressed destination well, subject to its initial volume and capacity. Tips may carry sample into the diluent source if reused; expose that consequence when asking about carryover. An explicit scientist tip policy does not establish a reviewed liquid class or effective capacity. Do not combine additions from different sources into one aspiration. Leave unknown inventories, liquid methods, well heights, physical plate identities and fresh-tip supply unresolved. The recipe is synthetic planning guidance, not a qualified method.

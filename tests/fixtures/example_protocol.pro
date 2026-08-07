<?xml version='1.0' encoding='ASCII'?>
<Protocol_File Version="1.0">
  <!--
    Synthetic .pro fixture for tests/test_legacy_protocol_import.py.

    This is an invented protocol, not a capture of any real run. It exists only
    to exercise every branch of pybravo/workflow/legacy_protocol_import.py:
      * File_Info metadata + StartScript
      * startup JavaScript variable extraction (int / float / string / [] / bare)
      * plate definitions (source, destination, tip box) with lids + single-instance
      * barcode-scan detection via Place Plate to location 6
      * a non-pipette control process with Loop / Spawn Process / Loop End
      * a Pipette_Process with Set Head Mode, Tips On, Aspirate, Dispense, Tips Off
      * embedded (entity-escaped) head-mode and well-selection XML
      * per-task Advanced_Settings estimated time
      * disabled and skipped tasks that must be dropped from the workflow
  -->
  <File_Info
      Description="Stamp a 96-well source plate into two 384-well assay plates."
      Device_File="ExampleDeck.dev"
      StartScript="var runMode = 'demo';" />

  <Processes>

    <!-- ============================ STARTUP ============================ -->
    <Startup_Processes>
      <Process Name="Initialize Run">
        <Task Name="Run JavaScript">
          <Parameters>
            <Parameter Name="Task description" Value="Declare the run variables" />
          </Parameters>
          <TaskScript Value="var loopIndex; var plateCounter = 1; var srcBC = []; var dispVol = 2.5; var numPlates = 3; var runMode = &quot;stamp&quot;;" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>
    </Startup_Processes>

    <!-- ============================= MAIN ============================== -->
    <Main_Processes>

      <!-- ====================== source plate ====================== -->
      <Process Name="Source Plate">
        <Plate_Parameters>
          <Parameter Name="Plate name" Value="Source Plate" />
          <Parameter Name="Plate type" Value="96 Well Polypropylene Plate" />
          <Parameter Name="Use single instance of plate" Value="1" />
          <Parameter Name="Plates have lids" Value="0" />
        </Plate_Parameters>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Move the source plate to the pipetting position" />
            <Parameter Name="Location to use" Value="4" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Remove Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Return the source plate to the stack" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <!-- ===================== assay plate one ==================== -->
      <Process Name="Assay Plate 1">
        <Plate_Parameters>
          <Parameter Name="Plate name" Value="Assay Plate 1" />
          <Parameter Name="Plate type" Value="384 Well Assay Plate" />
          <Parameter Name="Use single instance of plate" Value="0" />
          <Parameter Name="Plates have lids" Value="1" />
        </Plate_Parameters>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Move the plate to the reader" />
            <Parameter Name="Location to use" Value="6" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Run JavaScript">
          <Parameters>
            <Parameter Name="Task description" Value="Capture the scanned label" />
          </Parameters>
          <TaskScript Value="destBC1 = plate.barcode[EAST];" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Move the plate to the dispense position" />
            <Parameter Name="Location to use" Value="7" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <!-- ===================== assay plate two ==================== -->
      <Process Name="Assay Plate 2">
        <Plate_Parameters>
          <Parameter Name="Plate name" Value="Assay Plate 2" />
          <Parameter Name="Plate type" Value="384 Well Assay Plate" />
          <Parameter Name="Use single instance of plate" Value="0" />
          <Parameter Name="Plates have lids" Value="1" />
        </Plate_Parameters>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Move the plate to the reader" />
            <Parameter Name="Location to use" Value="6" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Run JavaScript">
          <Parameters>
            <Parameter Name="Task description" Value="Capture the scanned label" />
          </Parameters>
          <TaskScript Value="destBC2 = plate.barcode[EAST];" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Move the plate to the dispense position" />
            <Parameter Name="Location to use" Value="8" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <!-- ======================== tip boxes ======================= -->
      <Process Name="Tip Box 1">
        <Plate_Parameters>
          <Parameter Name="Plate name" Value="Tip Box 1" />
          <Parameter Name="Plate type" Value="384 Well Disposable Tip Box" />
          <Parameter Name="Use single instance of plate" Value="0" />
          <Parameter Name="Plates have lids" Value="0" />
        </Plate_Parameters>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Stage the first tip box" />
            <Parameter Name="Location to use" Value="2" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <Process Name="Tip Box 2">
        <Plate_Parameters>
          <Parameter Name="Plate name" Value="Tip Box 2" />
          <Parameter Name="Plate type" Value="384 Well Disposable Tip Box" />
          <Parameter Name="Use single instance of plate" Value="0" />
          <Parameter Name="Plates have lids" Value="0" />
        </Plate_Parameters>
        <Task Name="Place Plate">
          <Parameters>
            <Parameter Name="Task description" Value="Stage the second tip box" />
            <Parameter Name="Location to use" Value="3" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <!-- ====================== loop controller =================== -->
      <Process Name="Stamp Control">
        <Task Name="Loop">
          <Parameters>
            <Parameter Name="Task description" Value="Repeat the stamp once per source plate" />
            <Parameter Name="Number of times to loop" Value="1" />
          </Parameters>
          <TaskScript Value="task.Numberoftimestoloop = numPlates;" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="0" />
          </Advanced_Settings>
        </Task>
        <Task Name="Spawn Process">
          <Parameters>
            <Parameter Name="Task description" Value="Run the pipetting subprocess" />
            <Parameter Name="Process to spawn" Value="Plate Stamp" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
        <Task Name="Loop End">
          <Parameters>
            <Parameter Name="Task description" Value="End of the per-plate loop" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>

      <!-- ===================== pipetting process ================== -->
      <Pipette_Process Name="Plate Stamp">

        <Task Name="Set Head Mode">
          <Parameters>
            <Parameter Name="Task description" Value="Use the front-left 8x12 rectangle of barrels" />
            <Parameter Name="Head mode" Value='&lt;HeadModeInfo&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;/HeadModeInfo&gt;' />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="0.5" />
          </Advanced_Settings>
        </Task>

        <Task Name="Tips On">
          <Parameters>
            <Parameter Name="Task description" Value="Mount tips from the first box" />
            <Parameter Name="Location, plate" Value="Tip Box 1" />
            <Parameter Name="Location, location" Value="2" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="0" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <TaskScript Value="if (plateCounter != 1) { task.skip(); }" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="12.5" />
          </Advanced_Settings>
        </Task>

        <Task Name="Tips On">
          <Parameters>
            <Parameter Name="Task description" Value="Mount tips from the second box" />
            <Parameter Name="Location, plate" Value="Tip Box 2" />
            <Parameter Name="Location, location" Value="3" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="0" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <TaskScript Value="if (plateCounter != 2) { task.skip(); }" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="12.5" />
          </Advanced_Settings>
        </Task>

        <Task Name="Aspirate">
          <Parameters>
            <Parameter Name="Task description" Value="Draw sample for the first assay plate" />
            <Parameter Name="Location, plate" Value="Source Plate" />
            <Parameter Name="Location, location" Value="4" />
            <Parameter Name="Volume" Value="dispVol" />
            <Parameter Name="Pre-aspirate volume" Value="4" />
            <Parameter Name="Post-aspirate volume" Value="1.5" />
            <Parameter Name="Liquid class" Value="Aqueous Low Volume" />
            <Parameter Name="Distance from well bottom" Value="0.3" />
            <Parameter Name="Dynamic tip extension" Value="0.1" />
            <Parameter Name="Perform tip touch" Value="0" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="0" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <PipetteHead>
            <PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" />
          </PipetteHead>
          <TaskScript Value="task.Volume = dispVol;" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="20" />
          </Advanced_Settings>
        </Task>

        <Task Name="Dispense">
          <Parameters>
            <Parameter Name="Task description" Value="Deliver sample into the first assay plate" />
            <Parameter Name="Location, plate" Value="Assay Plate 1" />
            <Parameter Name="Location, location" Value="7" />
            <Parameter Name="Volume" Value="dispVol" />
            <Parameter Name="Blowout volume" Value="2" />
            <Parameter Name="Empty tips" Value="1" />
            <Parameter Name="Liquid class" Value="Aqueous Low Volume" />
            <Parameter Name="Distance from well bottom" Value="0.2" />
            <Parameter Name="Dynamic tip retraction" Value="0.05" />
            <Parameter Name="Perform tip touch" Value="1" />
            <Parameter Name="Which sides to use for tip touch" Value="North and South" />
            <Parameter Name="Tip touch retract distance" Value="1.5" />
            <Parameter Name="Tip touch horizontal offset" Value="0.25" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="1" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;Well Row="0" Column="1" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <TaskScript Value="if(plateCounter == 1) task.Wellselection = [[1,1]]; if(plateCounter == 2) task.Wellselection = [[1,2]]; if(plateCounter == 3) task.Wellselection = [[2,1]];" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="18" />
          </Advanced_Settings>
        </Task>

        <Task Name="Aspirate">
          <Parameters>
            <Parameter Name="Task description" Value="Draw sample for the second assay plate" />
            <Parameter Name="Location, plate" Value="Source Plate" />
            <Parameter Name="Location, location" Value="4" />
            <Parameter Name="Volume" Value="dispVol" />
            <Parameter Name="Pre-aspirate volume" Value="4" />
            <Parameter Name="Liquid class" Value="Aqueous Low Volume" />
            <Parameter Name="Distance from well bottom" Value="0.3" />
            <Parameter Name="Perform tip touch" Value="0" />
          </Parameters>
          <TaskScript Value="task.Volume = dispVol;" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="20" />
          </Advanced_Settings>
        </Task>

        <Task Name="Dispense">
          <Parameters>
            <Parameter Name="Task description" Value="Deliver sample into the second assay plate" />
            <Parameter Name="Location, plate" Value="Assay Plate 2" />
            <Parameter Name="Location, location" Value="8" />
            <Parameter Name="Volume" Value="dispVol" />
            <Parameter Name="Blowout volume" Value="2" />
            <Parameter Name="Empty tips" Value="1" />
            <Parameter Name="Liquid class" Value="Aqueous Low Volume" />
            <Parameter Name="Distance from well bottom" Value="0.2" />
            <Parameter Name="Perform tip touch" Value="0" />
          </Parameters>
          <TaskScript Value="task.Volume = dispVol;" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="18" />
          </Advanced_Settings>
        </Task>

        <Task Name="Tips Off">
          <Parameters>
            <Parameter Name="Task description" Value="Return tips to the first box" />
            <Parameter Name="Location, plate" Value="Tip Box 1" />
            <Parameter Name="Location, location" Value="2" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="0" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <TaskScript Value="if (plateCounter != 1) { task.skip(); }" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="10" />
          </Advanced_Settings>
        </Task>

        <Task Name="Tips Off">
          <Parameters>
            <Parameter Name="Task description" Value="Return tips to the second box" />
            <Parameter Name="Location, plate" Value="Tip Box 2" />
            <Parameter Name="Location, location" Value="3" />
            <Parameter Name="Well selection" Value='&lt;WellSelectionInfo&gt;&lt;WellSelection IsQuadrantPattern="0" StartingQuadrant="1"&gt;&lt;PipetteHeadMode SubsetType="4" SubsetConfig="2" Channels="96" RowCount="8" ColumnCount="12" TipType="2" /&gt;&lt;Wells&gt;&lt;Well Row="0" Column="0" /&gt;&lt;/Wells&gt;&lt;/WellSelection&gt;&lt;/WellSelectionInfo&gt;' />
          </Parameters>
          <TaskScript Value="if (plateCounter != 2) { task.skip(); }" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
          <Advanced_Settings>
            <Setting Name="Estimated time" Value="10" />
          </Advanced_Settings>
        </Task>

        <!-- Optional wash cycle, switched off in this protocol. Both tasks must
             be parsed but must not appear in the generated workflow steps. -->
        <Task Name="Aspirate">
          <Parameters>
            <Parameter Name="Task description" Value="Optional wash aspirate" />
            <Parameter Name="Location, plate" Value="Wash Reservoir" />
            <Parameter Name="Location, location" Value="9" />
            <Parameter Name="Volume" Value="50" />
          </Parameters>
          <Task_Disabled>1</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>

        <Task Name="Dispense">
          <Parameters>
            <Parameter Name="Task description" Value="Optional wash dispense" />
            <Parameter Name="Location, plate" Value="Waste" />
            <Parameter Name="Location, location" Value="1" />
            <Parameter Name="Volume" Value="50" />
          </Parameters>
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>1</Task_Skipped>
        </Task>

      </Pipette_Process>

    </Main_Processes>

    <!-- ============================ CLEANUP ============================ -->
    <Cleanup_Processes>
      <Process Name="Shutdown">
        <Task Name="Run JavaScript">
          <Parameters>
            <Parameter Name="Task description" Value="Report that the run finished" />
          </Parameters>
          <TaskScript Value="print('run complete');" />
          <Task_Disabled>0</Task_Disabled>
          <Task_Skipped>0</Task_Skipped>
        </Task>
      </Process>
    </Cleanup_Processes>

  </Processes>
</Protocol_File>

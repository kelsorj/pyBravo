import { useEffect, useMemo, useState, Suspense } from 'react'
import { Canvas, useLoader } from '@react-three/fiber'
import { OrbitControls, Html, Bounds, useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader'
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader'

const DEFAULT_API_URL = `${window.location.protocol}//${window.location.hostname}:8091`
const API_URL = import.meta.env.VITE_API_URL || DEFAULT_API_URL

function sortByName(a, b) {
  return String(a?.name || '').localeCompare(String(b?.name || ''))
}

function uniq(arr) {
  return Array.from(new Set(arr))
}

function TabButton({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '10px 14px',
        borderRadius: 8,
        border: '1px solid #333',
        background: active ? '#1677ff' : '#1a1a2e',
        color: '#fff',
        fontWeight: 'bold',
        cursor: 'pointer'
      }}
    >
      {children}
    </button>
  )
}

function ModelPreview({ url, format }) {
  const fmt = String(format || '').toLowerCase()

  if (fmt === 'glb' || fmt === 'gltf') {
    const { scene } = useGLTF(url)
    const cloned = useMemo(() => scene.clone(), [scene])

    useEffect(() => {
      cloned.traverse((child) => {
        if (child?.isMesh) {
          child.castShadow = true
          child.receiveShadow = true
          if (child.material) child.material.side = THREE.DoubleSide
        }
      })
    }, [cloned])

    return <primitive object={cloned} />
  }

  if (fmt === 'stl') {
    const geo = useLoader(STLLoader, url)
    const mat = useMemo(() => new THREE.MeshStandardMaterial({ color: 0x9dcfe9, roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide }), [])
    return <mesh geometry={geo} material={mat} castShadow receiveShadow />
  }

  if (fmt === 'obj') {
    const obj = useLoader(OBJLoader, url)
    const cloned = useMemo(() => obj.clone(), [obj])
    useEffect(() => {
      cloned.traverse((child) => {
        if (child?.isMesh) {
          child.castShadow = true
          child.receiveShadow = true
          if (child.material) child.material.side = THREE.DoubleSide
        }
      })
    }, [cloned])
    return <primitive object={cloned} />
  }

  return (
    <Html center>
      <div style={{ color: '#ddd' }}>No preview available for: {format}</div>
    </Html>
  )
}

function SmallButton({ disabled, onClick, children, variant = 'default' }) {
  const bg = variant === 'danger' ? '#ff4d4f' : variant === 'primary' ? '#52c41a' : '#444'
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      style={{
        padding: '9px 12px',
        borderRadius: 8,
        border: 'none',
        background: bg,
        color: '#fff',
        fontWeight: 'bold',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.6 : 1,
        whiteSpace: 'nowrap'
      }}
    >
      {children}
    </button>
  )
}

function labwareHelpImage(filename) {
  return `/static/labware_images/${encodeURIComponent(filename)}`
}

const LABWARE_PROPERTY_HELP = {
  robot_gripper_offset_mm: {
    title: 'Robot gripper offset',
    caption: 'Distance from the bottom of the labware up to the gripper contact plane. Distance from the top down to the grip plane = thickness - offset.',
    src: labwareHelpImage('robot gripper offset.svg'),
  },
  robot_lid_gripper_offset_mm: {
    title: 'Lid gripper offset',
    caption: 'Distance from the bottom of the standalone lid up to the gripper contact plane used for delid/relid.',
  },
  stacking_thickness_mm: {
    title: 'Stacking thickness',
    caption: 'Reference for the stack increment between identical plates.',
  },
  sealed_stacking_thickness_mm: {
    title: 'Sealed stacking thickness',
    caption: 'Uses the same stacking-thickness concept, but for the sealed state.',
  },
  lidded_thickness_mm: {
    title: 'Lidded thickness',
    caption: 'Closest matching lid-height reference currently available in the editor.',
  },
  lidded_stacking_thickness_mm: {
    title: 'Lidded stacking thickness',
    caption: 'Uses the same stacking-thickness concept, but for the lidded state.',
  },
  lid_resting_height_mm: {
    title: 'Lid resting height',
    caption: 'Reference for the lid resting-height measurement.',
  },
  lid_departure_height_mm: {
    title: 'Lid departure height',
    caption: 'Reference for the lid departure-height measurement.',
  },
  filter_tip_pin_tool_length_mm: {
    title: 'Filter tip/pin tool length',
    caption: 'Reference for measuring the length of the filter tip or pin tool.',
  },
  filter_channel_resting_depth_mm: {
    title: 'Filter channel resting depth',
    caption: 'Reference for the filter channel resting-depth measurement.',
  },
}

function HoverHelpLabel({ helpKey, style, children }) {
  const help = LABWARE_PROPERTY_HELP[helpKey]
  const [preview, setPreview] = useState({ open: false, x: 0, y: 0 })

  if (!help) {
    return <div style={style}>{children}</div>
  }

  const placePreview = (event) => {
    const viewportWidth = window.innerWidth || 1280
    const viewportHeight = window.innerHeight || 720
    const nextX = Math.min(event.clientX + 18, Math.max(16, viewportWidth - 440))
    const nextY = Math.min(event.clientY + 18, Math.max(16, viewportHeight - 360))
    setPreview({ open: true, x: nextX, y: nextY })
  }

  const showFromKeyboard = () => {
    const viewportWidth = window.innerWidth || 1280
    setPreview({
      open: true,
      x: Math.max(16, Math.min(viewportWidth - 440, 32)),
      y: 32,
    })
  }

  return (
    <>
      <div
        tabIndex={0}
        onMouseEnter={placePreview}
        onMouseMove={placePreview}
        onMouseLeave={() => setPreview(prev => ({ ...prev, open: false }))}
        onFocus={showFromKeyboard}
        onBlur={() => setPreview(prev => ({ ...prev, open: false }))}
        style={{
          ...style,
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          gap: 8,
          cursor: 'help',
          textDecoration: 'underline dotted #78a6ff',
          textUnderlineOffset: '0.18em',
          outline: 'none',
        }}
      >
        <span>{children}</span>
        <span
          aria-hidden="true"
          style={{
            width: 18,
            height: 18,
            borderRadius: '50%',
            border: '1px solid #4f6fb8',
            background: '#18233f',
            color: '#b8ccff',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 11,
            fontWeight: 'bold',
            flexShrink: 0,
          }}
        >
          ?
        </span>
      </div>
      {preview.open ? (
        <div
          style={{
            position: 'fixed',
            left: preview.x,
            top: preview.y,
            width: 'min(420px, calc(100vw - 32px))',
            background: 'rgba(10, 14, 24, 0.97)',
            border: '1px solid #44557f',
            borderRadius: 12,
            boxShadow: '0 16px 44px rgba(0, 0, 0, 0.45)',
            padding: 12,
            zIndex: 1000,
            pointerEvents: 'none',
          }}
        >
          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 6 }}>{help.title}</div>
          <div style={{ color: '#b7bfd4', fontSize: 12, lineHeight: 1.45, marginBottom: help.src ? 10 : 0 }}>{help.caption}</div>
          {help.src ? (
            <img
              src={help.src}
              alt={help.title}
              style={{
                display: 'block',
                width: '100%',
                height: 'auto',
                borderRadius: 8,
                border: '1px solid #33405f',
                background: '#fff',
              }}
            />
          ) : null}
        </div>
      ) : null}
    </>
  )
}

function LabwareDashboard() {
  const [mode, setMode] = useState('entries') // entries | classes
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const [labwareTypes, setLabwareTypes] = useState([])
  const [labwareClasses, setLabwareClasses] = useState([])
  const [loading, setLoading] = useState(true)

  const [selectedTypeId, setSelectedTypeId] = useState('')
  const [selectedClassId, setSelectedClassId] = useState('')
  const [entryTab, setEntryTab] = useState('plate') // plate | pipette | image | pf400 | planar

  // Entries filtering (left list)
  const [wellsFilter, setWellsFilter] = useState('all') // 'all' | 6 | 24 | 48 | 96 | 384 | 1536

  // New entry form (minimal)
  const [newEntryName, setNewEntryName] = useState('')
  const [newEntryWells, setNewEntryWells] = useState(96)
  const wellsOptions = useMemo(() => [6, 24, 48, 96, 384, 1536], [])

  // New class form (minimal)
  const [newClassName, setNewClassName] = useState('')

  const card = useMemo(() => ({
    background: '#1a1a2e',
    borderRadius: 12,
    border: '1px solid #333',
    padding: 16,
  }), [])

  const input = useMemo(() => ({
    padding: '8px 10px',
    borderRadius: 8,
    border: '1px solid #555',
    background: '#2a2a3e',
    color: '#fff',
    outline: 'none',
    width: '100%',
    boxSizing: 'border-box'
  }), [])

  const label = useMemo(() => ({ fontSize: '0.85em', color: '#bbb', marginBottom: 6, fontWeight: 'bold' }), [])
  const fieldLabel = useMemo(() => ({ color: '#bbb', textAlign: 'right' }), [])

  const fetchAll = async () => {
    try {
      setLoading(true)
      const [typesRes, classesRes] = await Promise.all([
        fetch(`${API_URL}/labware/types`),
        fetch(`${API_URL}/labware/classes`),
      ])
      const typesJson = await typesRes.json().catch(() => ({}))
      const classesJson = await classesRes.json().catch(() => ({}))
      if (!typesRes.ok) throw new Error(typesJson.detail || 'Failed to fetch labware types')
      if (!classesRes.ok) throw new Error(classesJson.detail || 'Failed to fetch labware classes')
      const types = (typesJson.labware_types || []).slice().sort(sortByName)
      const classes = (classesJson.labware_classes || []).slice().sort(sortByName)
      setLabwareTypes(types)
      setLabwareClasses(classes)
      setError('')

      // choose defaults
      if (!selectedTypeId && types.length) setSelectedTypeId(types[0].labware_type_id)
      if (!selectedClassId && classes.length) setSelectedClassId(classes[0].labware_class_id)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
  }, [])

  const selectedType = useMemo(
    () => labwareTypes.find(t => t.labware_type_id === selectedTypeId) || null,
    [labwareTypes, selectedTypeId]
  )

  const filteredLabwareTypes = useMemo(() => {
    if (wellsFilter === 'all') return labwareTypes
    const w = Number(wellsFilter)
    return labwareTypes.filter(t => Number(t?.wells) === w)
  }, [labwareTypes, wellsFilter])

  useEffect(() => {
    // If current selection is filtered out, select the first visible entry (or clear).
    if (mode !== 'entries') return
    if (selectedTypeId && filteredLabwareTypes.some(t => t.labware_type_id === selectedTypeId)) return
    if (filteredLabwareTypes.length) setSelectedTypeId(filteredLabwareTypes[0].labware_type_id)
    else setSelectedTypeId('')
  }, [mode, wellsFilter, labwareTypes])

  const selectedClass = useMemo(
    () => labwareClasses.find(c => c.labware_class_id === selectedClassId) || null,
    [labwareClasses, selectedClassId]
  )

  const createEntry = async () => {
    setBusy(true)
    try {
      const payload = {
        kind: 'sbs_plate',
        name: newEntryName.trim(),
        wells: Number(newEntryWells),
        plate_dimensions_mm: { length_mm: 127.76, width_mm: 85.48, height_mm: 0 },
        well_dimensions_mm: {},
      }
      const res = await fetch(`${API_URL}/labware/types`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to create labware entry')
      setNewEntryName('')
      await fetchAll()
      const createdId = data?.labware_type?.labware_type_id
      if (createdId) setSelectedTypeId(createdId)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const deleteEntry = async () => {
    if (!selectedType) return
    if (!confirm(`Delete labware entry "${selectedType.name}"?`)) return
    setBusy(true)
    try {
      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, { method: 'DELETE' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to delete labware entry')
      setSelectedTypeId('')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const renameEntry = async () => {
    if (!selectedType) return
    const nextName = prompt('Rename labware entry:', selectedType.name)
    if (!nextName || !nextName.trim() || nextName.trim() === selectedType.name) return
    setBusy(true)
    try {
      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nextName.trim() }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to rename labware entry')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  // -------- Plate Properties tab state (editable) --------
  const [ppDescription, setPpDescription] = useState('')
  const [ppManufacturerPart, setPpManufacturerPart] = useState('')
  const [ppWells, setPpWells] = useState(96)
  const [ppBaseClass, setPpBaseClass] = useState('microplate')

  const [ppRobotGripperOffset, setPpRobotGripperOffset] = useState('')
  const [ppRobotLidGripperOffset, setPpRobotLidGripperOffset] = useState('')
  const [ppEmptyCheckOffset, setPpEmptyCheckOffset] = useState('')
  const [ppThickness, setPpThickness] = useState('')
  const [ppStackingThickness, setPpStackingThickness] = useState('')
  const [ppShimThickness, setPpShimThickness] = useState('')

  const [ppCanBeSealed, setPpCanBeSealed] = useState(false)
  const [ppSealedThickness, setPpSealedThickness] = useState('')
  const [ppSealedStackingThickness, setPpSealedStackingThickness] = useState('')

  const [ppCanHaveLid, setPpCanHaveLid] = useState(false)
  const [ppLiddedThickness, setPpLiddedThickness] = useState('')
  const [ppLiddedStackingThickness, setPpLiddedStackingThickness] = useState('')
  const [ppLidRestingHeight, setPpLidRestingHeight] = useState('')
  const [ppLidDepartureHeight, setPpLidDepartureHeight] = useState('')

  const [ppLowerAtLabeler, setPpLowerAtLabeler] = useState(false)
  const [ppCanMount, setPpCanMount] = useState(false)
  const [ppCanBeMounted, setPpCanBeMounted] = useState(false)
  const [ppHandlingSpeed, setPpHandlingSpeed] = useState('fast') // slow|medium|fast

  const [ppFilterTipPinToolLength, setPpFilterTipPinToolLength] = useState('')
  const [ppFilterChannelRestingDepth, setPpFilterChannelRestingDepth] = useState('')
  const [ppRequiresInsert, setPpRequiresInsert] = useState('None')

  useEffect(() => {
    // Load editor state when the selected entry changes
    if (!selectedType) return
    setEntryTab('plate')
    setPpDescription(selectedType.description || '')
    setPpManufacturerPart(selectedType.catalog_number || '')
    setPpWells(Number(selectedType.wells || 96))
    setPpBaseClass(selectedType.base_class || 'microplate')

    const pp = selectedType.plate_properties || {}
    const f = (v) => (v === null || v === undefined) ? '' : String(v)
    setPpRobotGripperOffset(f(pp.robot_gripper_offset_mm))
    setPpRobotLidGripperOffset(f(pp.robot_lid_gripper_offset_mm))
    setPpEmptyCheckOffset(f(pp.empty_check_offset_mm))
    setPpThickness(f(pp.thickness_mm ?? selectedType?.plate_dimensions_mm?.height_mm))
    setPpStackingThickness(f(pp.stacking_thickness_mm))
    setPpShimThickness(f(pp.shim_thickness_mm))

    setPpCanBeSealed(Boolean(pp.can_be_sealed))
    setPpSealedThickness(f(pp.sealed_thickness_mm))
    setPpSealedStackingThickness(f(pp.sealed_stacking_thickness_mm))

    setPpCanHaveLid(Boolean(pp.can_have_lid))
    setPpLiddedThickness(f(pp.lidded_thickness_mm))
    setPpLiddedStackingThickness(f(pp.lidded_stacking_thickness_mm))
    setPpLidRestingHeight(f(pp.lid_resting_height_mm))
    setPpLidDepartureHeight(f(pp.lid_departure_height_mm))

    setPpLowerAtLabeler(Boolean(pp.lower_plate_at_labeler))
    setPpCanMount(Boolean(pp.can_mount))
    setPpCanBeMounted(Boolean(pp.can_be_mounted))
    setPpHandlingSpeed(pp.max_robot_handling_speed || 'fast')

    setPpFilterTipPinToolLength(f(pp.filter_tip_pin_tool_length_mm))
    setPpFilterChannelRestingDepth(f(pp.filter_channel_resting_depth_mm))
    setPpRequiresInsert(pp.requires_insert || 'None')

    // PF400 gripper (legacy "BenchBot Gripper")
    const pf = selectedType.pf400 || {}
    setPf400LandscapeRanges(f(pf.landscape_gripping_ranges_mm))
    setPf400LandscapeOpen(f(pf.landscape_open_width_mm))
    setPf400LandscapeClosed(f(pf.landscape_closed_width_mm))
    setPf400LandscapeTol(f(pf.landscape_tolerance_mm))
    setPf400PortraitRanges(f(pf.portrait_gripping_ranges_mm))
    setPf400PortraitOpen(f(pf.portrait_open_width_mm))
    setPf400PortraitClosed(f(pf.portrait_closed_width_mm))
    setPf400PortraitTol(f(pf.portrait_tolerance_mm))
    setPf400Torque(f(pf.grip_torque_percent))

    // Planar Motor limits
    const pm = selectedType.planar_motor || {}
    setPlanarMaxVel(f(pm.max_velocity_m_per_s))
    setPlanarMaxAccel(f(pm.max_acceleration_m_per_s2))
  }, [selectedTypeId])

  // -------- Pipette/Well Definition tab state (editable) --------
  const [wdVolumeUl, setWdVolumeUl] = useState('')
  const [wdDepthMm, setWdDepthMm] = useState('')
  const [wdDiameterMm, setWdDiameterMm] = useState('')
  const [wdOffsetX, setWdOffsetX] = useState('')
  const [wdOffsetY, setWdOffsetY] = useState('')
  const [wdPitchX, setWdPitchX] = useState('')
  const [wdPitchY, setWdPitchY] = useState('')
  const [wdGeometry, setWdGeometry] = useState(1) // 1=round, 2=square
  const [wdBottomShape, setWdBottomShape] = useState(2) // 1=rounded, 2=flat, 3=v-shaped

  const [tipSource, setTipSource] = useState('agilent') // agilent | third_party
  const [tipCapacityUl, setTipCapacityUl] = useState(10)
  const [thirdPartyTipCapacityUl, setThirdPartyTipCapacityUl] = useState('')
  const [disposableTipLengthMm, setDisposableTipLengthMm] = useState('')

  // -------- Image tab state (upload) --------
  const [imageFile, setImageFile] = useState(null)
  const [modelFile, setModelFile] = useState(null)

  // -------- PF400 tab state (gripper) --------
  const [pf400LandscapeRanges, setPf400LandscapeRanges] = useState('')
  const [pf400LandscapeOpen, setPf400LandscapeOpen] = useState('')
  const [pf400LandscapeClosed, setPf400LandscapeClosed] = useState('')
  const [pf400LandscapeTol, setPf400LandscapeTol] = useState('')
  const [pf400PortraitRanges, setPf400PortraitRanges] = useState('')
  const [pf400PortraitOpen, setPf400PortraitOpen] = useState('')
  const [pf400PortraitClosed, setPf400PortraitClosed] = useState('')
  const [pf400PortraitTol, setPf400PortraitTol] = useState('')
  const [pf400Torque, setPf400Torque] = useState('')

  // -------- Planar Motor tab state --------
  const [planarMaxVel, setPlanarMaxVel] = useState('')
  const [planarMaxAccel, setPlanarMaxAccel] = useState('')

  useEffect(() => {
    if (!selectedType) return
    const wd = selectedType.well_dimensions_mm || {}
    const f = (v) => (v === null || v === undefined) ? '' : String(v)
    setWdVolumeUl(f(wd.volume_ul))
    setWdDepthMm(f(wd.depth_mm))
    setWdDiameterMm(f(wd.diameter_mm))
    setWdOffsetX(f(wd.offset_x_mm))
    setWdOffsetY(f(wd.offset_y_mm))
    setWdPitchX(f(wd.spacing_x_mm))
    setWdPitchY(f(wd.spacing_y_mm))
    setWdGeometry(Number(wd.well_geometry || 1))
    setWdBottomShape(Number(wd.well_bottom_shape || 2))

    setTipSource(wd.tip_source || 'agilent')
    const cap = wd.disposable_tip_capacity_ul
    setTipCapacityUl(Number.isFinite(Number(cap)) ? Number(cap) : 10)
    setThirdPartyTipCapacityUl(f(cap))
    setDisposableTipLengthMm(f(wd.disposable_tip_length_mm))
  }, [selectedTypeId])

  const saveWellDefinition = async () => {
    if (!selectedType) return
    setBusy(true)
    try {
      const n = (s) => {
        const t = String(s ?? '').trim()
        if (t === '') return null
        const v = Number(t)
        return Number.isFinite(v) ? v : null
      }

      const capacity = tipSource === 'third_party' ? n(thirdPartyTipCapacityUl) : Number(tipCapacityUl)

      const payload = {
        well_dimensions_mm: {
          ...(selectedType.well_dimensions_mm || {}),
          volume_ul: n(wdVolumeUl),
          depth_mm: n(wdDepthMm),
          diameter_mm: n(wdDiameterMm),
          offset_x_mm: n(wdOffsetX),
          offset_y_mm: n(wdOffsetY),
          spacing_x_mm: n(wdPitchX),
          spacing_y_mm: n(wdPitchY),
          well_geometry: Number(wdGeometry),
          well_bottom_shape: Number(wdBottomShape),
          tip_source: tipSource,
          disposable_tip_capacity_ul: capacity,
          disposable_tip_length_mm: n(disposableTipLengthMm),
        }
      }

      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to save well definition')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const savePlateProperties = async () => {
    if (!selectedType) return
    setBusy(true)
    try {
      const n = (s) => {
        const t = String(s || '').trim()
        if (t === '') return null
        const v = Number(t)
        return Number.isFinite(v) ? v : null
      }

      const payload = {
        description: ppDescription,
        catalog_number: ppManufacturerPart,
        base_class: ppBaseClass,
        wells: Number(ppWells),
        plate_properties: {
          robot_gripper_offset_mm: n(ppRobotGripperOffset),
          robot_lid_gripper_offset_mm: n(ppRobotLidGripperOffset),
          empty_check_offset_mm: n(ppEmptyCheckOffset),
          thickness_mm: n(ppThickness),
          stacking_thickness_mm: n(ppStackingThickness),
          shim_thickness_mm: n(ppShimThickness),
          can_be_sealed: Boolean(ppCanBeSealed),
          sealed_thickness_mm: n(ppSealedThickness),
          sealed_stacking_thickness_mm: n(ppSealedStackingThickness),
          can_have_lid: Boolean(ppCanHaveLid),
          lidded_thickness_mm: n(ppLiddedThickness),
          lidded_stacking_thickness_mm: n(ppLiddedStackingThickness),
          lid_resting_height_mm: n(ppLidRestingHeight),
          lid_departure_height_mm: n(ppLidDepartureHeight),
          lower_plate_at_labeler: Boolean(ppLowerAtLabeler),
          can_mount: Boolean(ppCanMount),
          can_be_mounted: Boolean(ppCanBeMounted),
          max_robot_handling_speed: ppHandlingSpeed,
          filter_tip_pin_tool_length_mm: n(ppFilterTipPinToolLength),
          filter_channel_resting_depth_mm: n(ppFilterChannelRestingDepth),
          requires_insert: String(ppRequiresInsert || '').trim() || null,
        }
      }

      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to save plate properties')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const savePf400 = async () => {
    if (!selectedType) return
    setBusy(true)
    try {
      const n = (s) => {
        const t = String(s ?? '').trim()
        if (t === '') return null
        const v = Number(t)
        return Number.isFinite(v) ? v : null
      }

      const payload = {
        pf400: {
          landscape_gripping_ranges_mm: String(pf400LandscapeRanges || '').trim() || null,
          landscape_open_width_mm: n(pf400LandscapeOpen),
          landscape_closed_width_mm: n(pf400LandscapeClosed),
          landscape_tolerance_mm: n(pf400LandscapeTol),
          portrait_gripping_ranges_mm: String(pf400PortraitRanges || '').trim() || null,
          portrait_open_width_mm: n(pf400PortraitOpen),
          portrait_closed_width_mm: n(pf400PortraitClosed),
          portrait_tolerance_mm: n(pf400PortraitTol),
          grip_torque_percent: n(pf400Torque),
        }
      }

      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to save PF400 settings')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const savePlanarMotor = async () => {
    if (!selectedType) return
    setBusy(true)
    try {
      const n = (s) => {
        const t = String(s ?? '').trim()
        if (t === '') return null
        const v = Number(t)
        return Number.isFinite(v) ? v : null
      }

      const payload = {
        planar_motor: {
          max_velocity_m_per_s: n(planarMaxVel),
          max_acceleration_m_per_s2: n(planarMaxAccel),
        }
      }

      const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to save Planar Motor settings')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const createClass = async () => {
    setBusy(true)
    try {
      const res = await fetch(`${API_URL}/labware/classes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newClassName.trim(), description: '' })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to create labware class')
      setNewClassName('')
      await fetchAll()
      const createdId = data?.labware_class?.labware_class_id
      if (createdId) setSelectedClassId(createdId)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const renameClass = async () => {
    if (!selectedClass) return
    const nextName = prompt('Rename labware class:', selectedClass.name)
    if (!nextName || !nextName.trim()) return
    setBusy(true)
    try {
      const res = await fetch(`${API_URL}/labware/classes/${encodeURIComponent(selectedClass.labware_class_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nextName.trim() })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to rename labware class')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const deleteClass = async () => {
    if (!selectedClass) return
    if (!confirm(`Delete labware class "${selectedClass.name}"? This will remove it from all labware entries.`)) return
    setBusy(true)
    try {
      const res = await fetch(`${API_URL}/labware/classes/${encodeURIComponent(selectedClass.labware_class_id)}`, { method: 'DELETE' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to delete labware class')
      setSelectedClassId('')
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  // Membership editing on class screen (replicate classes.PNG):
  // stage changes locally, then commit via Save changes.
  const actualMemberIds = useMemo(() => {
    if (!selectedClass) return []
    const cid = selectedClass.labware_class_id
    return labwareTypes
      .filter(t => (t.labware_class_ids || []).includes(cid))
      .map(t => t.labware_type_id)
      .sort()
  }, [labwareTypes, selectedClass])

  const [stagedMemberIds, setStagedMemberIds] = useState([])
  const [pickedNonMembers, setPickedNonMembers] = useState([])
  const [pickedMembers, setPickedMembers] = useState([])

  useEffect(() => {
    // reset when switching class
    setPickedNonMembers([])
    setPickedMembers([])
    setStagedMemberIds(actualMemberIds)
  }, [selectedClassId, actualMemberIds.join('|')])

  const stagedMemberSet = useMemo(() => new Set(stagedMemberIds), [stagedMemberIds])
  const hasUnsavedMembershipChanges = useMemo(() => {
    const a = actualMemberIds.join('|')
    const b = stagedMemberIds.slice().sort().join('|')
    return a !== b
  }, [actualMemberIds, stagedMemberIds])

  const classMembers = useMemo(() => {
    return labwareTypes
      .filter(t => stagedMemberSet.has(t.labware_type_id))
      .slice()
      .sort(sortByName)
  }, [labwareTypes, stagedMemberSet])

  const classNonMembers = useMemo(() => {
    return labwareTypes
      .filter(t => !stagedMemberSet.has(t.labware_type_id))
      .slice()
      .sort(sortByName)
  }, [labwareTypes, stagedMemberSet])

  const addSelectedToStaged = () => {
    if (pickedNonMembers.length === 0) return
    setStagedMemberIds(prev => uniq([...prev, ...pickedNonMembers]))
    setPickedNonMembers([])
  }

  const removeSelectedFromStaged = () => {
    if (pickedMembers.length === 0) return
    const remove = new Set(pickedMembers)
    setStagedMemberIds(prev => prev.filter(id => !remove.has(id)))
    setPickedMembers([])
  }

  const addAllToStaged = () => {
    setStagedMemberIds(labwareTypes.map(t => t.labware_type_id))
    setPickedNonMembers([])
  }

  const removeAllFromStaged = () => {
    setStagedMemberIds([])
    setPickedMembers([])
  }

  const patchTypeClasses = async (typeId, newClassIds) => {
    const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(typeId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ labware_class_ids: newClassIds })
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || 'Failed to update membership')
    return data.labware_type
  }

  const saveClassMembership = async () => {
    if (!selectedClass) return
    const cid = selectedClass.labware_class_id
    setBusy(true)
    try {
      const desired = new Set(stagedMemberIds)
      const updates = []
      for (const t of labwareTypes) {
        const cur = new Set(t.labware_class_ids || [])
        const shouldHave = desired.has(t.labware_type_id)
        const has = cur.has(cid)
        if (shouldHave === has) continue
        if (shouldHave) cur.add(cid)
        else cur.delete(cid)
        updates.push({ id: t.labware_type_id, classIds: Array.from(cur) })
      }
      for (const u of updates) {
        await patchTypeClasses(u.id, u.classIds)
      }
      await fetchAll()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const saveClassAs = async () => {
    if (!selectedClass) return
    const nextName = prompt('Save changes as (new class name):', `${selectedClass.name}-copy`)
    if (!nextName || !nextName.trim()) return
    setBusy(true)
    try {
      // create new class
      const res = await fetch(`${API_URL}/labware/classes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nextName.trim(), description: '' })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to create labware class')
      const newCid = data?.labware_class?.labware_class_id
      if (!newCid) throw new Error('Created labware class missing id')

      // apply membership for new class to the staged members only (do not alter original class)
      const desired = new Set(stagedMemberIds)
      for (const t of labwareTypes) {
        if (!desired.has(t.labware_type_id)) continue
        const cur = new Set(t.labware_class_ids || [])
        if (cur.has(newCid)) continue
        cur.add(newCid)
        await patchTypeClasses(t.labware_type_id, Array.from(cur))
      }

      await fetchAll()
      setSelectedClassId(newCid)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ padding: 20, maxWidth: 1500, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div />
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <TabButton active={mode === 'entries'} onClick={() => setMode('entries')}>Labware Entries</TabButton>
          <TabButton active={mode === 'classes'} onClick={() => setMode('classes')}>Labware Classes</TabButton>
          <SmallButton disabled={busy} onClick={fetchAll}>Refresh</SmallButton>
        </div>
      </div>

      {error && (
        <div style={{ ...card, borderColor: '#ff4d4f', marginBottom: 12 }}>
          <div style={{ color: '#ff7875', fontWeight: 'bold' }}>Error</div>
          <div style={{ color: '#ccc', marginTop: 6 }}>{error}</div>
        </div>
      )}

      {loading ? (
        <div style={{ color: '#888' }}>Loading…</div>
      ) : mode === 'entries' ? (
        <div style={{ display: 'grid', gridTemplateColumns: '340px 1fr', gap: 14, alignItems: 'start' }}>
          {/* Left: entries list + create */}
          <div style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <div style={{ color: '#fff', fontWeight: 'bold' }}>Labware Entries</div>
              <div style={{ color: '#888', fontSize: '0.9em' }}>
                {wellsFilter === 'all' ? `${labwareTypes.length} total` : `${filteredLabwareTypes.length} of ${labwareTypes.length}`}
              </div>
            </div>

            <div style={{ display: 'grid', gap: 10 }}>
              <div style={{ display: 'grid', gap: 6 }}>
                <div style={label}>Create new (SBS plate)</div>
                <input value={newEntryName} onChange={(e) => setNewEntryName(e.target.value)} style={input} placeholder="Name" disabled={busy} />
                <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8 }}>
                  <select value={newEntryWells} onChange={(e) => setNewEntryWells(Number(e.target.value))} style={input} disabled={busy}>
                    {wellsOptions.map(w => <option key={w} value={w}>{w} wells</option>)}
                  </select>
                  <SmallButton variant="primary" disabled={busy || !newEntryName.trim()} onClick={createEntry}>New</SmallButton>
                </div>
              </div>

              <div style={{ display: 'grid', gap: 6 }}>
                <div style={label}>Filter</div>
                <select value={wellsFilter} onChange={(e) => setWellsFilter(e.target.value)} style={input} disabled={busy}>
                  <option value="all">All</option>
                  {wellsOptions.map(w => <option key={w} value={String(w)}>{w} wells</option>)}
                </select>
              </div>

              <div style={{ height: 520, overflow: 'auto', border: '1px solid #333', borderRadius: 10, padding: 8, background: '#111' }}>
                {filteredLabwareTypes.map(t => (
                  <div
                    key={t.labware_type_id}
                    onClick={() => setSelectedTypeId(t.labware_type_id)}
                    style={{
                      padding: '8px 10px',
                      borderRadius: 8,
                      cursor: 'pointer',
                      background: t.labware_type_id === selectedTypeId ? '#1677ff33' : 'transparent',
                      border: t.labware_type_id === selectedTypeId ? '1px solid #1677ff' : '1px solid transparent',
                      color: '#fff'
                    }}
                  >
                    <div style={{ fontWeight: 'bold' }}>{t.name}</div>
                    <div style={{ color: '#888', fontSize: '0.85em', marginTop: 2 }}>
                      {t.wells ? `${t.wells} wells` : t.kind}
                      {t.vendor ? ` · ${t.vendor}` : ''}
                      {t.catalog_number ? ` · ${t.catalog_number}` : ''}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Right: entry details (first pass) */}
          <div style={card}>
            {!selectedType ? (
              <div style={{ color: '#888' }}>Select a labware entry to view/edit.</div>
            ) : (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                  <div>
                    <div style={{ color: '#fff', fontWeight: 'bold', fontSize: '1.2em' }}>{selectedType.name}</div>
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <SmallButton disabled={busy} onClick={renameEntry}>Rename</SmallButton>
                    <SmallButton variant="danger" disabled={busy} onClick={deleteEntry}>Delete</SmallButton>
                  </div>
                </div>

                {/* Entry tabs (starting with Plate Properties to match plate-props.PNG) */}
                <div style={{ display: 'flex', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
                  <TabButton active={entryTab === 'plate'} onClick={() => setEntryTab('plate')}>Plate Properties</TabButton>
                  <TabButton active={entryTab === 'pipette'} onClick={() => setEntryTab('pipette')}>Pipette/Well Definition</TabButton>
                  <TabButton active={entryTab === 'image'} onClick={() => setEntryTab('image')}>Image</TabButton>
                  <TabButton active={entryTab === 'pf400'} onClick={() => setEntryTab('pf400')}>PF400</TabButton>
                  <TabButton active={entryTab === 'planar'} onClick={() => setEntryTab('planar')}>Planar Motor</TabButton>
                </div>

                {entryTab === 'plate' ? (
                  <div style={{ marginTop: 14 }}>
                    {/* General */}
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 14 }}>
                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Labware-Entry General Properties</div>

                        <div style={{ display: 'grid', gap: 10 }}>
                          <div>
                            <div style={label}>Description</div>
                            <textarea
                              value={ppDescription}
                              onChange={(e) => setPpDescription(e.target.value)}
                              style={{ ...input, height: 90, resize: 'vertical' }}
                              disabled={busy}
                            />
                          </div>

                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px', gap: 10 }}>
                            <div>
                              <div style={label}>Manufacturer part number</div>
                              <input value={ppManufacturerPart} onChange={(e) => setPpManufacturerPart(e.target.value)} style={input} disabled={busy} />
                            </div>
                            <div>
                              <div style={label}>Number of wells</div>
                              <select value={ppWells} onChange={(e) => setPpWells(Number(e.target.value))} style={input} disabled={busy}>
                                {wellsOptions.map(w => <option key={w} value={w}>{w}</option>)}
                              </select>
                            </div>
                          </div>
                        </div>
                      </div>

                      {/* Base class */}
                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Base Class</div>
                        {[
                          ['microplate', 'Microplate'],
                          ['filter_plate', 'Filter plate'],
                          ['reservoir', 'Reservoir'],
                          ['tip_wash_station', 'Tip Wash Station'],
                          ['pin_tool', 'Pin tool'],
                          ['tip_box', 'Tip box'],
                          ['lid', 'Lid'],
                          ['tip_trash_bin', 'Tip trash bin'],
                          ['assaymap_cartridge_rack', 'AssayMAP cartridge rack'],
                        ].map(([val, text]) => (
                          <label key={val} style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                            <input
                              type="radio"
                              name="base_class"
                              value={val}
                              checked={ppBaseClass === val}
                              onChange={() => setPpBaseClass(val)}
                              disabled={busy}
                            />
                            {text}
                          </label>
                        ))}
                      </div>
                    </div>

                    {/* Plate properties grid like screenshot */}
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 14 }}>
                      {/* Left group: Plate thickness/stacking/lid/seal */}
                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Plate Properties</div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 140px', gap: 10, alignItems: 'center' }}>
                          <HoverHelpLabel helpKey="robot_gripper_offset_mm" style={fieldLabel}>Robot gripper offset (mm)</HoverHelpLabel>
                          <input value={ppRobotGripperOffset} onChange={(e) => setPpRobotGripperOffset(e.target.value)} style={input} disabled={busy} />

                          <HoverHelpLabel helpKey="robot_lid_gripper_offset_mm" style={fieldLabel}>Lid gripper offset (mm)</HoverHelpLabel>
                          <input value={ppRobotLidGripperOffset} onChange={(e) => setPpRobotLidGripperOffset(e.target.value)} style={input} disabled={busy || !ppCanHaveLid} />

                          <div style={fieldLabel}>Empty check offset (mm)</div>
                          <input value={ppEmptyCheckOffset} onChange={(e) => setPpEmptyCheckOffset(e.target.value)} style={input} disabled={busy} />

                          <div style={fieldLabel}>Thickness (mm)</div>
                          <input value={ppThickness} onChange={(e) => setPpThickness(e.target.value)} style={input} disabled={busy} />

                          <HoverHelpLabel helpKey="stacking_thickness_mm" style={fieldLabel}>Stacking thickness (mm)</HoverHelpLabel>
                          <input value={ppStackingThickness} onChange={(e) => setPpStackingThickness(e.target.value)} style={input} disabled={busy} />

                          <div style={fieldLabel}>Shim/nesting thickness (mm)</div>
                          <input value={ppShimThickness} onChange={(e) => setPpShimThickness(e.target.value)} style={input} disabled={busy} />

                          <div style={fieldLabel}>Can be sealed?</div>
                          <input type="checkbox" checked={ppCanBeSealed} onChange={(e) => setPpCanBeSealed(e.target.checked)} disabled={busy} />

                          <div style={fieldLabel}>Sealed thickness (mm)</div>
                          <input value={ppSealedThickness} onChange={(e) => setPpSealedThickness(e.target.value)} style={input} disabled={busy || !ppCanBeSealed} />

                          <HoverHelpLabel helpKey="sealed_stacking_thickness_mm" style={fieldLabel}>Sealed stacking thickness (mm)</HoverHelpLabel>
                          <input value={ppSealedStackingThickness} onChange={(e) => setPpSealedStackingThickness(e.target.value)} style={input} disabled={busy || !ppCanBeSealed} />

                          <div style={fieldLabel}>Can have lid?</div>
                          <input type="checkbox" checked={ppCanHaveLid} onChange={(e) => setPpCanHaveLid(e.target.checked)} disabled={busy} />

                          <HoverHelpLabel helpKey="lidded_thickness_mm" style={fieldLabel}>Lidded thickness (mm)</HoverHelpLabel>
                          <input value={ppLiddedThickness} onChange={(e) => setPpLiddedThickness(e.target.value)} style={input} disabled={busy || !ppCanHaveLid} />

                          <HoverHelpLabel helpKey="lidded_stacking_thickness_mm" style={fieldLabel}>Lidded stacking thickness (mm)</HoverHelpLabel>
                          <input value={ppLiddedStackingThickness} onChange={(e) => setPpLiddedStackingThickness(e.target.value)} style={input} disabled={busy || !ppCanHaveLid} />

                          <HoverHelpLabel helpKey="lid_resting_height_mm" style={fieldLabel}>Lid resting height (mm)</HoverHelpLabel>
                          <input value={ppLidRestingHeight} onChange={(e) => setPpLidRestingHeight(e.target.value)} style={input} disabled={busy || !ppCanHaveLid} />

                          <HoverHelpLabel helpKey="lid_departure_height_mm" style={fieldLabel}>Lid departure height (mm)</HoverHelpLabel>
                          <input value={ppLidDepartureHeight} onChange={(e) => setPpLidDepartureHeight(e.target.value)} style={input} disabled={busy || !ppCanHaveLid} />
                        </div>
                      </div>

                      {/* Right group: handling / speed / misc */}
                      <div style={{ display: 'grid', gap: 14 }}>
                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Plate Handling</div>
                          <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                            <input type="checkbox" checked={ppLowerAtLabeler} onChange={(e) => setPpLowerAtLabeler(e.target.checked)} disabled={busy} />
                            Lower plate at Microplate Labeler
                          </label>
                          <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                            <input type="checkbox" checked={ppCanMount} onChange={(e) => setPpCanMount(e.target.checked)} disabled={busy} />
                            Can mount
                          </label>
                          <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd' }}>
                            <input type="checkbox" checked={ppCanBeMounted} onChange={(e) => setPpCanBeMounted(e.target.checked)} disabled={busy} />
                            Can be mounted
                          </label>
                        </div>

                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Maximum Robot Handling Speed</div>
                          {[
                            ['slow', 'Slow'],
                            ['medium', 'Medium'],
                            ['fast', 'Fast'],
                          ].map(([val, text]) => (
                            <label key={val} style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                              <input
                                type="radio"
                                name="handling_speed"
                                value={val}
                                checked={ppHandlingSpeed === val}
                                onChange={() => setPpHandlingSpeed(val)}
                                disabled={busy}
                              />
                              {text}
                            </label>
                          ))}
                        </div>

                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Miscellaneous</div>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 140px', gap: 10, alignItems: 'center' }}>
                            <HoverHelpLabel helpKey="filter_tip_pin_tool_length_mm" style={fieldLabel}>Length of filter tip/pin tool (mm)</HoverHelpLabel>
                            <input value={ppFilterTipPinToolLength} onChange={(e) => setPpFilterTipPinToolLength(e.target.value)} style={input} disabled={busy} />
                            <HoverHelpLabel helpKey="filter_channel_resting_depth_mm" style={fieldLabel}>Filter channel resting depth (mm)</HoverHelpLabel>
                            <input value={ppFilterChannelRestingDepth} onChange={(e) => setPpFilterChannelRestingDepth(e.target.value)} style={input} disabled={busy} />
                          </div>

                          <div style={{ marginTop: 10 }}>
                            <div style={label}>Requires insert</div>
                            <select value={ppRequiresInsert} onChange={(e) => setPpRequiresInsert(e.target.value)} style={input} disabled={busy}>
                              <option value="None">None</option>
                              <option value="Required">Required</option>
                            </select>
                          </div>
                        </div>

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                          <SmallButton variant="primary" disabled={busy} onClick={savePlateProperties}>Save changes</SmallButton>
                        </div>
                      </div>
                    </div>
                  </div>
                ) : entryTab === 'pipette' ? (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                      {/* Well Dimensions */}
                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Well Dimensions</div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 10, alignItems: 'center' }}>
                          <div style={fieldLabel}>Well volume (uL)</div>
                          <input value={wdVolumeUl} onChange={(e) => setWdVolumeUl(e.target.value)} style={input} disabled={busy} />
                          <div style={fieldLabel}>Well depth (mm)</div>
                          <input value={wdDepthMm} onChange={(e) => setWdDepthMm(e.target.value)} style={input} disabled={busy} />
                          <div style={fieldLabel}>Well diameter (mm)</div>
                          <input value={wdDiameterMm} onChange={(e) => setWdDiameterMm(e.target.value)} style={input} disabled={busy} />
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 14 }}>
                          <div style={{ border: '1px solid #333', borderRadius: 12, padding: 12 }}>
                            <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 8 }}>Well Geometry</div>
                            <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                              <input type="radio" name="well_geom" checked={Number(wdGeometry) === 1} onChange={() => setWdGeometry(1)} disabled={busy} />
                              Round
                            </label>
                            <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd' }}>
                              <input type="radio" name="well_geom" checked={Number(wdGeometry) === 2} onChange={() => setWdGeometry(2)} disabled={busy} />
                              Square
                            </label>
                          </div>

                          <div style={{ border: '1px solid #333', borderRadius: 12, padding: 12 }}>
                            <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 8 }}>Well-Bottom Shape</div>
                            <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                              <input type="radio" name="well_bottom" checked={Number(wdBottomShape) === 1} onChange={() => setWdBottomShape(1)} disabled={busy} />
                              Rounded
                            </label>
                            <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                              <input type="radio" name="well_bottom" checked={Number(wdBottomShape) === 2} onChange={() => setWdBottomShape(2)} disabled={busy} />
                              Flat
                            </label>
                            <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd' }}>
                              <input type="radio" name="well_bottom" checked={Number(wdBottomShape) === 3} onChange={() => setWdBottomShape(3)} disabled={busy} />
                              V-Shaped
                            </label>
                          </div>
                        </div>
                      </div>

                      {/* Well Positions + Tip Parameters */}
                      <div style={{ display: 'grid', gap: 14 }}>
                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Well Positions</div>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 10, alignItems: 'center' }}>
                            <div style={fieldLabel}>Row-wise teachpoint to well (mm)</div>
                            <input value={wdOffsetX} onChange={(e) => setWdOffsetX(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Column-wise teachpoint to well (mm)</div>
                            <input value={wdOffsetY} onChange={(e) => setWdOffsetY(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Row-wise well to well (mm)</div>
                            <input value={wdPitchX} onChange={(e) => setWdPitchX(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Column-wise well to well (mm)</div>
                            <input value={wdPitchY} onChange={(e) => setWdPitchY(e.target.value)} style={input} disabled={busy} />
                          </div>
                        </div>

                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Tip Parameters</div>
                          <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                            <input type="radio" name="tip_source" checked={tipSource === 'agilent'} onChange={() => setTipSource('agilent')} disabled={busy} />
                            Agilent Technologies tip box
                          </label>
                          {tipSource === 'agilent' && (
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 10, alignItems: 'center', marginBottom: 10 }}>
                              <div style={fieldLabel}>Disposable tip capacity (uL)</div>
                              <select value={tipCapacityUl} onChange={(e) => setTipCapacityUl(Number(e.target.value))} style={input} disabled={busy}>
                                {[10, 50, 60, 200, 250, 300].map(v => <option key={v} value={v}>{v} uL</option>)}
                              </select>
                            </div>
                          )}

                          <label style={{ display: 'flex', gap: 10, alignItems: 'center', color: '#ddd', marginBottom: 8 }}>
                            <input type="radio" name="tip_source" checked={tipSource === 'third_party'} onChange={() => setTipSource('third_party')} disabled={busy} />
                            3rd party tip box
                          </label>
                          {tipSource === 'third_party' && (
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 10, alignItems: 'center', marginBottom: 10 }}>
                              <div style={fieldLabel}>Disposable tip capacity (uL)</div>
                              <input value={thirdPartyTipCapacityUl} onChange={(e) => setThirdPartyTipCapacityUl(e.target.value)} style={input} disabled={busy} />
                            </div>
                          )}

                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 10, alignItems: 'center' }}>
                            <div style={fieldLabel}>Disposable tip length (mm)</div>
                            <input value={disposableTipLengthMm} onChange={(e) => setDisposableTipLengthMm(e.target.value)} style={input} disabled={busy} />
                          </div>
                        </div>

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                          <SmallButton variant="primary" disabled={busy} onClick={saveWellDefinition}>Save changes</SmallButton>
                        </div>
                      </div>
                    </div>
                  </div>
                ) : entryTab === 'pf400' ? (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                      <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>PF400 Gripper</div>

                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 12 }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 8 }}>Landscape</div>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px', gap: 10, alignItems: 'center' }}>
                            <div style={fieldLabel}>Orientation gripping ranges (mm)</div>
                            <input value={pf400LandscapeRanges} onChange={(e) => setPf400LandscapeRanges(e.target.value)} style={input} disabled={busy} placeholder="e.g. 2-6" />
                            <div style={{ gridColumn: '1 / span 2', color: '#666', fontSize: '0.85em', textAlign: 'right' }}>
                              Examples: "0-2" or "1-3, 2.5, 4-8"
                            </div>
                            <div style={fieldLabel}>Open width (mm)</div>
                            <input value={pf400LandscapeOpen} onChange={(e) => setPf400LandscapeOpen(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Closed width (mm)</div>
                            <input value={pf400LandscapeClosed} onChange={(e) => setPf400LandscapeClosed(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Tolerance (mm)</div>
                            <input value={pf400LandscapeTol} onChange={(e) => setPf400LandscapeTol(e.target.value)} style={input} disabled={busy} />
                          </div>
                        </div>

                        <div style={{ border: '1px solid #333', borderRadius: 12, padding: 12 }}>
                          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 8 }}>Portrait</div>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px', gap: 10, alignItems: 'center' }}>
                            <div style={fieldLabel}>Orientation gripping ranges (mm)</div>
                            <input value={pf400PortraitRanges} onChange={(e) => setPf400PortraitRanges(e.target.value)} style={input} disabled={busy} placeholder="e.g. 2.5-6" />
                            <div style={{ gridColumn: '1 / span 2', color: '#666', fontSize: '0.85em', textAlign: 'right' }}>
                              Examples: "0-2" or "1-3, 2.5, 4-8"
                            </div>
                            <div style={fieldLabel}>Open width (mm)</div>
                            <input value={pf400PortraitOpen} onChange={(e) => setPf400PortraitOpen(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Closed width (mm)</div>
                            <input value={pf400PortraitClosed} onChange={(e) => setPf400PortraitClosed(e.target.value)} style={input} disabled={busy} />
                            <div style={fieldLabel}>Tolerance (mm)</div>
                            <input value={pf400PortraitTol} onChange={(e) => setPf400PortraitTol(e.target.value)} style={input} disabled={busy} />
                          </div>
                        </div>
                      </div>

                      <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 180px', gap: 10, alignItems: 'center' }}>
                        <div style={fieldLabel}>Grip torque (% of maximum)</div>
                        <input value={pf400Torque} onChange={(e) => setPf400Torque(e.target.value)} style={input} disabled={busy} />
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 14 }}>
                        <SmallButton variant="primary" disabled={busy} onClick={savePf400}>Save changes</SmallButton>
                      </div>
                    </div>
                  </div>
                ) : entryTab === 'planar' ? (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                      <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Planar Motor</div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 180px', gap: 10, alignItems: 'center' }}>
                        <div style={fieldLabel}>Max velocity (m/s)</div>
                        <input value={planarMaxVel} onChange={(e) => setPlanarMaxVel(e.target.value)} style={input} disabled={busy} />

                        <div style={fieldLabel}>Max acceleration (m/s²)</div>
                        <input value={planarMaxAccel} onChange={(e) => setPlanarMaxAccel(e.target.value)} style={input} disabled={busy} />
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 14 }}>
                        <SmallButton variant="primary" disabled={busy} onClick={savePlanarMotor}>Save changes</SmallButton>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 14 }}>
                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Image</div>

                        <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr 90px', gap: 10, alignItems: 'center' }}>
                          <div style={{ color: '#bbb', textAlign: 'right' }}>Image filename</div>
                          <input
                            value={selectedType?.image_2d?.url ? `${API_URL}${selectedType.image_2d.url}` : (imageFile?.name || '')}
                            readOnly
                            style={input}
                          />
                          <label style={{ margin: 0 }}>
                            <input
                              type="file"
                              accept="image/png,image/jpeg,image/webp,image/gif"
                              style={{ display: 'none' }}
                              onChange={(e) => setImageFile(e.target.files?.[0] || null)}
                              disabled={busy}
                            />
                            <SmallButton disabled={busy} onClick={(e) => e.currentTarget.parentElement.querySelector('input[type=file]').click()}>…</SmallButton>
                          </label>
                        </div>

                        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 10 }}>
                          <SmallButton
                            variant="primary"
                            disabled={busy || !selectedType || !imageFile}
                            onClick={async () => {
                              if (!selectedType || !imageFile) return
                              setBusy(true)
                              try {
                                const fd = new FormData()
                                fd.append('file', imageFile)
                                const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}/assets/image`, {
                                  method: 'POST',
                                  body: fd,
                                })
                                const data = await res.json().catch(() => ({}))
                                if (!res.ok) throw new Error(data.detail || 'Failed to upload image')
                                setImageFile(null)
                                await fetchAll()
                              } catch (e) {
                                setError(e.message || String(e))
                              } finally {
                                setBusy(false)
                              }
                            }}
                          >
                            Upload image
                          </SmallButton>
                        </div>
                      </div>

                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>3D Model (optional)</div>
                        <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr 90px', gap: 10, alignItems: 'center' }}>
                          <div style={{ color: '#bbb', textAlign: 'right' }}>Model filename</div>
                          <input
                            value={selectedType?.model_3d?.url ? `${API_URL}${selectedType.model_3d.url}` : (modelFile?.name || '')}
                            readOnly
                            style={input}
                          />
                          <label style={{ margin: 0 }}>
                            <input
                              type="file"
                              accept=".stl,.obj,.glb,.gltf,.grbl"
                              style={{ display: 'none' }}
                              onChange={(e) => setModelFile(e.target.files?.[0] || null)}
                              disabled={busy}
                            />
                            <SmallButton disabled={busy} onClick={(e) => e.currentTarget.parentElement.querySelector('input[type=file]').click()}>…</SmallButton>
                          </label>
                        </div>

                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, marginTop: 10 }}>
                          <div style={{ color: '#888', fontSize: '0.9em' }}>
                            Supported: <span style={{ color: '#bbb' }}>STL, OBJ, GLB, GLTF</span>
                            <div style={{ marginTop: 4, color: '#666' }}>
                              Tip: prefer <span style={{ color: '#bbb' }}>.glb</span> for uploads (single file). <span style={{ color: '#bbb' }}>.gltf</span> often needs a separate <span style={{ color: '#bbb' }}>.bin</span> + textures.
                            </div>
                          </div>
                          <SmallButton
                            variant="primary"
                            disabled={busy || !selectedType || !modelFile}
                            onClick={async () => {
                              if (!selectedType || !modelFile) return
                              setBusy(true)
                              try {
                                const fd = new FormData()
                                fd.append('file', modelFile)
                                const res = await fetch(`${API_URL}/labware/types/${encodeURIComponent(selectedType.labware_type_id)}/assets/model`, {
                                  method: 'POST',
                                  body: fd,
                                })
                                const data = await res.json().catch(() => ({}))
                                if (!res.ok) throw new Error(data.detail || 'Failed to upload model')
                                setModelFile(null)
                                await fetchAll()
                              } catch (e) {
                                setError(e.message || String(e))
                              } finally {
                                setBusy(false)
                              }
                            }}
                          >
                            Upload model
                          </SmallButton>
                        </div>
                      </div>

                      <div style={{ border: '1px solid #333', borderRadius: 12, padding: 14, background: '#111', minHeight: 420 }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: 10 }}>Preview</div>
                        {selectedType?.image_2d?.url ? (
                          <img
                            src={`${API_URL}${selectedType.image_2d.url}`}
                            alt="labware preview"
                            style={{ maxWidth: '100%', maxHeight: 360, objectFit: 'contain', borderRadius: 10, border: '1px solid #222' }}
                          />
                        ) : (
                          <div style={{ color: '#666' }}>No image uploaded yet.</div>
                        )}
                        {selectedType?.model_3d?.url ? (
                          <>
                            <div style={{ marginTop: 12, color: '#aaa' }}>
                              3D model: <span style={{ color: '#ccc' }}>{selectedType.model_3d.format}</span> ·{' '}
                              <a href={`${API_URL}${selectedType.model_3d.url}`} target="_blank" rel="noreferrer" style={{ color: '#69c0ff' }}>
                                open file
                              </a>
                            </div>
                            <div style={{ marginTop: 10, height: 360, borderRadius: 10, overflow: 'hidden', border: '1px solid #222' }}>
                              <Canvas
                                camera={{ position: [0.7, 0.5, 0.7], fov: 45, near: 0.01, far: 200 }}
                                shadows
                              >
                                <color attach="background" args={['#0b0b0f']} />
                                <ambientLight intensity={0.65} />
                                <directionalLight position={[3, 5, 2]} intensity={0.9} castShadow />
                                <directionalLight position={[-3, 3, -2]} intensity={0.35} />
                                <Suspense fallback={<Html center><div style={{ color: '#ddd' }}>Loading 3D…</div></Html>}>
                                  <Bounds fit clip observe margin={1.2}>
                                    <ModelPreview
                                      url={`${API_URL}${selectedType.model_3d.url}`}
                                      format={selectedType.model_3d.format}
                                    />
                                  </Bounds>
                                </Suspense>
                                <OrbitControls makeDefault />
                              </Canvas>
                            </div>
                          </>
                        ) : null}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 14, alignItems: 'start' }}>
          {/* Left: classes list */}
          <div style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <div style={{ color: '#fff', fontWeight: 'bold' }}>Labware Classes</div>
              <div style={{ color: '#888', fontSize: '0.9em' }}>{labwareClasses.length} total</div>
            </div>

            <div style={{ display: 'grid', gap: 10 }}>
              <div style={{ display: 'grid', gap: 6 }}>
                <div style={label}>New class</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8 }}>
                  <input value={newClassName} onChange={(e) => setNewClassName(e.target.value)} style={input} placeholder="e.g. ShortLid" disabled={busy} />
                  <SmallButton variant="primary" disabled={busy || !newClassName.trim()} onClick={createClass}>New</SmallButton>
                </div>
              </div>

              <div style={{ height: 520, overflow: 'auto', border: '1px solid #333', borderRadius: 10, padding: 8, background: '#111' }}>
                {labwareClasses.length === 0 ? (
                  <div style={{ color: '#888' }}>No classes yet.</div>
                ) : labwareClasses.map(c => (
                  <div
                    key={c.labware_class_id}
                    onClick={() => setSelectedClassId(c.labware_class_id)}
                    style={{
                      padding: '8px 10px',
                      borderRadius: 8,
                      cursor: 'pointer',
                      background: c.labware_class_id === selectedClassId ? '#1677ff33' : 'transparent',
                      border: c.labware_class_id === selectedClassId ? '1px solid #1677ff' : '1px solid transparent',
                      color: '#fff'
                    }}
                  >
                    <div style={{ fontWeight: 'bold' }}>{c.name}</div>
                    <div style={{ color: '#666', fontSize: '0.8em', fontFamily: 'monospace', marginTop: 2 }}>
                      {c.labware_class_id}
                    </div>
                  </div>
                ))}
              </div>

              <div style={{ display: 'flex', gap: 10 }}>
                <SmallButton disabled={busy || !selectedClass} onClick={renameClass}>Rename</SmallButton>
                <SmallButton variant="danger" disabled={busy || !selectedClass} onClick={deleteClass}>Delete</SmallButton>
              </div>
            </div>
          </div>

          {/* Right: membership editor (matches screenshot layout) */}
          <div style={card}>
            {!selectedClass ? (
              <div style={{ color: '#888' }}>Select a labware class to edit membership.</div>
            ) : (
              <>
                <div style={{ color: '#fff', fontWeight: 'bold', fontSize: '1.1em' }}>
                  Labware-Entry Membership — <span style={{ color: '#69c0ff' }}>{selectedClass.name}</span>
                </div>
                <div style={{ color: '#888', marginTop: 6 }}>
                  Move labware entries between the lists (this controls device-fit constraints later).
                </div>
                <div style={{ marginTop: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10 }}>
                  <div style={{ color: hasUnsavedMembershipChanges ? '#faad14' : '#888', fontWeight: hasUnsavedMembershipChanges ? 'bold' : 'normal' }}>
                    {hasUnsavedMembershipChanges ? 'Unsaved changes' : 'No unsaved changes'}
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <SmallButton disabled={busy || !hasUnsavedMembershipChanges} onClick={saveClassMembership}>Save changes</SmallButton>
                    <SmallButton disabled={busy} onClick={saveClassAs}>Save changes as…</SmallButton>
                  </div>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 1fr', gap: 12, marginTop: 14 }}>
                  <div>
                    <div style={label}>Not a member of this class</div>
                    <select
                      multiple
                      value={pickedNonMembers}
                      onChange={(e) => setPickedNonMembers(Array.from(e.target.selectedOptions).map(o => o.value))}
                      style={{ ...input, height: 430 }}
                      disabled={busy}
                    >
                      {classNonMembers.map(t => (
                        <option key={t.labware_type_id} value={t.labware_type_id}>{t.name}</option>
                      ))}
                    </select>
                  </div>

                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10, justifyContent: 'center' }}>
                    <SmallButton disabled={busy || classNonMembers.length === 0} onClick={addAllToStaged}>&gt;&gt;</SmallButton>
                    <SmallButton disabled={busy || pickedNonMembers.length === 0} onClick={addSelectedToStaged}>&gt;</SmallButton>
                    <SmallButton disabled={busy || pickedMembers.length === 0} onClick={removeSelectedFromStaged}>&lt;</SmallButton>
                    <SmallButton disabled={busy || classMembers.length === 0} onClick={removeAllFromStaged}>&lt;&lt;</SmallButton>
                  </div>

                  <div>
                    <div style={label}>Member of this class</div>
                    <select
                      multiple
                      value={pickedMembers}
                      onChange={(e) => setPickedMembers(Array.from(e.target.selectedOptions).map(o => o.value))}
                      style={{ ...input, height: 430 }}
                      disabled={busy}
                    >
                      {classMembers.map(t => (
                        <option key={t.labware_type_id} value={t.labware_type_id}>{t.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default LabwareDashboard

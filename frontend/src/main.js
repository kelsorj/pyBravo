import * as THREE from 'three';
import * as SkeletonUtils from 'three/addons/utils/SkeletonUtils.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

// ── State ──────────────────────────────────────────────────────────────

const state = {
    theme: 'dark',
    connected: false,
    positions: { X: 0, Y: 0, Z: 0, W: 0, G: 0, Zg: 0 },
    renderPositions: { X: 0, Y: 0, Z: 0, W: 0, G: 0, Zg: 0 },
    motorsEnabled: {},
    telemetry: {},
    taskStatus: {},
    motionTargets: {},
    deckDetails: {},
    teachpoints: {},
    deckMotionMap: null,
    deckDetailsSignature: '',
    headAttached: false,
    headType: null,
    headMode: null,
    tipSelection: null,
    processWellSelection: {},
    processWellLegalAnchors: {},
    processWellLegalitySignatures: {},
    processWellLegalityLoading: {},
    tipboxInventory: {},
    tipsOnHead: false,
    tipLabware: '',
    attachedTipLengthMm: null,
    machineId: '',
    visionEnabled: false,
    visionServiceUrl: 'http://127.0.0.1:8101',
    visionSdkRoot: 'external/pyorbbecsdk',
    activeTipId: '',
    activeTipCapacityUl: null,
    tipsOnHeadMode: null,
    tipsOnHeadSelection: null,
    goButtonPressed: false,
    plateInGripper: false,
    robotDisabled: false,
    profileLoaded: false,
    accessoryDevices: [],
    selectedAccessoryId: '',
    ws: null,
    apiBase: `${location.protocol}//${location.hostname}:8000`,
    labwareCatalog: [],
    tipDefinitions: [],
    liquidClasses: [],
    pipetteTechniques: [],
    liquidContextSignature: '',
    pickPlaceTelemetryActive: false,
    lastTelemetryLogAt: 0,
    lastTelemetrySignature: '',
    pickupPromptSignature: '',
    taskPromptSignature: '',
    taskPromptActionPending: false,
    taskPromptPendingDetails: '',
    lastInitializeWHomingLogPosition: null,
    lastApiError: '',
    commandRunning: false,
    commandRunningAt: 0,
};

const MOTION_BUTTON_IDS = [
    'btn-exec-command',
    'btn-home',
    'btn-init',
    'btn-home-x', 'btn-home-y', 'btn-home-z', 'btn-home-w',
    'btn-home-g', 'btn-home-zg', 'btn-home-xyz',
    'btn-tp-move', 'btn-tp-approach', 'btn-tp-safe-z',
    'btn-open-gripper', 'btn-close-gripper', 'btn-dock-gripper',
    'btn-pick-ab', 'btn-pick-ba',
    'btn-grip-move', 'btn-grip-approach',
];

// Buttons that clear the readiness gate they would otherwise be blocked by.
// They still belong in MOTION_BUTTON_IDS, because the busy-state lockout below
// does apply to them — only the readiness gating has to skip them.
const SELF_REMEDY_BUTTON_IDS = new Set(['btn-init', 'btn-home']);

function setMotionButtonsEnabled(enabled) {
    // Busy state only. Readiness is a separate axis handled by applyReadiness,
    // which uses a class so the control stays hoverable and can explain itself.
    for (const id of MOTION_BUTTON_IDS) {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
    }
    document.querySelectorAll('.jog-btn[data-jog]').forEach(el => {
        if (enabled) {
            el.classList.remove('motion-disabled');
        } else {
            el.classList.add('motion-disabled');
        }
    });
}

const MOTION_ANIMATION = {
    // 50 Hz cutoff: render reaches ~99% of target in ~90 ms.
    // Was 12 Hz, which took ~380 ms and ate the entire scan-stack-height
    // transient — Zg would swing -20 → +50 → -20 in three /ws/state
    // samples (~1.5 s total), but at 12 Hz render only managed a ~5 mm
    // visible excursion because each sample's peak was abandoned before
    // the lerp caught up. At 50 Hz render chases each sample faithfully.
    smoothingHz: 50,
    snapDistanceMm: 0.02,
};
const THEME_STORAGE_KEY = 'pybravo-theme';
const HEAD_CARRIAGE_VISUAL_Y_OFFSET_M = 0.008;
const FINGER_VISUAL_Z_OFFSET_M = 0.012;
const FINGER_JOINT_Y_OFFSET_M = -0.010;
const TOOLING_ASSEMBLY_VISUAL_Y_OFFSET_M = -0.006;
const LABWARE_CARRY_CLEARANCE_M = 0.04;
const DECK_SLOT_SURFACE_OFFSET_M = 0.005;
const DEFAULT_TELESHAKE_MODEL_PATH = '/static/accessories/TeleshakeSimple.gltf';
const DECK_POSITION_GREYS = [
    0xf3f3f6,
    0xe5e5ea,
    0xd6d7de,
    0xc8c9d1,
    0xb9bbc5,
    0xabacb8,
    0x9c9ea9,
    0x8e909b,
    0x7f828d,
];
const LABWARE_APPEARANCE_OVERRIDES = {
    '384 Greiner 781091 PS uclear': {
        color: 0xf2f2ee,
        transparent: false,
        opacity: 1.0,
        roughness: 0.88,
        metalness: 0.0,
        transmission: 0.0,
    },
};
const TELESHAKE_MATERIALS = {
    top: new THREE.MeshStandardMaterial({
        color: 0xf1f0eb,
        roughness: 0.58,
        metalness: 0.02,
    }),
    side: new THREE.MeshStandardMaterial({
        color: 0xdedbd3,
        roughness: 0.62,
        metalness: 0.04,
    }),
    base: new THREE.MeshStandardMaterial({
        color: 0xa9a397,
        roughness: 0.38,
        metalness: 0.38,
    }),
    blue: new THREE.MeshStandardMaterial({
        color: 0x1266ad,
        roughness: 0.36,
        metalness: 0.06,
    }),
};

const HEAD_TIP_OPTIONS = {
    HT_384_D_70: [
        { tip_id: 'st_10ul', capacity_ul: 10, label: '10 uL', length_mm: 19.9, model_3d: '/labware-assets/tips/d10.gltf?v=tips2' },
        { tip_id: 'st_15ul', capacity_ul: 15, label: '15 uL', length_mm: null, model_3d: '/labware-assets/tips/d10.gltf?v=tips2' },
        { tip_id: 'st_30ul', capacity_ul: 30, label: '30 uL', length_mm: 26.1, model_3d: '/labware-assets/tips/d30.gltf?v=tips2' },
        { tip_id: 'st_50ul', capacity_ul: 50, label: '50 uL', length_mm: null, model_3d: '/labware-assets/tips/d30.gltf?v=tips2' },
        { tip_id: 'st_51ul', capacity_ul: 51, label: '51 uL', length_mm: null, model_3d: '/labware-assets/tips/d30.gltf?v=tips2' },
        { tip_id: 'st_70ul', capacity_ul: 70, label: '70 uL', length_mm: null, model_3d: '/labware-assets/tips/d30.gltf?v=tips2' },
    ],
    HT_384_D_70_S2: [],
    HT_96_D_70: [],
    HT_96_D_70_S2: [],
    HT_96_D_200: [
        { tip_id: 'lt_200ul', capacity_ul: 200, label: '200 uL', length_mm: null, model_3d: null },
        { tip_id: 'lt_250ul', capacity_ul: 250, label: '250 uL', length_mm: null, model_3d: null },
    ],
    HT_96_D_200_S2: [],
    HT_8_D_LT: [],
};
HEAD_TIP_OPTIONS.HT_384_D_70_S2 = HEAD_TIP_OPTIONS.HT_384_D_70;
HEAD_TIP_OPTIONS.HT_96_D_70 = HEAD_TIP_OPTIONS.HT_384_D_70;
HEAD_TIP_OPTIONS.HT_96_D_70_S2 = HEAD_TIP_OPTIONS.HT_384_D_70;
HEAD_TIP_OPTIONS.HT_96_D_200_S2 = HEAD_TIP_OPTIONS.HT_96_D_200;
HEAD_TIP_OPTIONS.HT_8_D_LT = HEAD_TIP_OPTIONS.HT_96_D_200;

function getTipDefinitionForSelection(headType, ref, options = null) {
    const defs = options || HEAD_TIP_OPTIONS[headType] || [];
    return defs.find(t => String(t.tip_id || '') === String(ref ?? '') || String(t.capacity_ul) === String(ref ?? '')) || null;
}

function getTipHeightForCapacity(headType, capacity, options = null) {
    const match = getTipDefinitionForSelection(headType, capacity, options);
    return match ? match.length_mm : null;
}

function populateTeachTipOptions(headType, selectedCapacity, options = null) {
    const select = document.getElementById('tp-tip-capacity');
    const label = document.getElementById('tp-tip-height-label');
    if (!select) return;
    const defs = options || HEAD_TIP_OPTIONS[headType] || [{ capacity_ul: 200, label: '200 uL', length_mm: null }];
    select.innerHTML = defs.map(t => `<option value="${t.tip_id || t.capacity_ul}" data-capacity="${t.capacity_ul || 0}">${t.label}</option>`).join('');
    const effective = String(selectedCapacity || defs[0]?.tip_id || defs[0]?.capacity_ul || '');
    if (defs.map(t => String(t.tip_id || t.capacity_ul)).includes(effective)) {
        select.value = effective;
    }
    if (label) {
        const height = getTipHeightForCapacity(headType, select.value, defs);
        label.textContent = height == null ? 'unknown' : `${height.toFixed(1)} mm`;
    }
}

const HEAD_GEOMETRY = {
    HT_384_D_70: { rows: 16, columns: 24 },
    HT_384_D_70_S2: { rows: 16, columns: 24 },
    HT_384_F_50: { rows: 16, columns: 24 },
    HT_384_PINTOOL: { rows: 16, columns: 24 },
    HT_1536_PINTOOL: { rows: 32, columns: 48 },
    HT_16_D_ST: { rows: 16, columns: 1 },
    HT_8_D_LT: { rows: 8, columns: 1 },
};

function getHeadGeometry(headType) {
    return HEAD_GEOMETRY[headType] || { rows: 8, columns: 12 };
}

function normalizeHeadModeForUi(headType, headMode = null) {
    const geometry = getHeadGeometry(headType);
    let subsetType = String(headMode?.subset_type || 'all_barrels');
    let subsetConfig = String(headMode?.subset_config || 'back_left');
    if (!['front_left', 'front_right', 'back_left', 'back_right'].includes(subsetConfig)) {
        subsetConfig = 'back_left';
    }
    if (subsetType === 'quadrant') {
        subsetType = 'rectangle';
    }
    if (subsetType === 'all_barrels') {
        subsetConfig = 'back_left';
    }
    let rowCount = geometry.rows;
    let columnCount = geometry.columns;
    if (subsetType === 'row') {
        rowCount = Math.max(1, Math.min(geometry.rows, Number(headMode?.row_count || 1)));
    } else if (subsetType === 'column') {
        columnCount = Math.max(1, Math.min(geometry.columns, Number(headMode?.column_count || 1)));
    } else if (subsetType === 'rectangle') {
        rowCount = Math.max(1, Math.min(geometry.rows, Number(headMode?.row_count || Math.floor(geometry.rows / 2) || 1)));
        columnCount = Math.max(1, Math.min(geometry.columns, Number(headMode?.column_count || Math.floor(geometry.columns / 2) || 1)));
    } else if (subsetType === 'single_barrel') {
        rowCount = 1;
        columnCount = 1;
    }
    return {
        subset_type: subsetType,
        subset_config: subsetConfig,
        row_count: rowCount,
        column_count: columnCount,
        num_channels: rowCount * columnCount,
        display_text: describeHeadMode({
            subset_type: subsetType,
            subset_config: subsetConfig,
        }),
    };
}

function describeHeadMode(headMode) {
    if (!headMode || headMode.subset_type === 'all_barrels') return 'All barrels';
    const subsetLabels = {
        row: 'Full row',
        column: 'Full column',
        rectangle: 'Rectangle',
        single_barrel: 'Single barrel',
    };
    const orientation = String(headMode.subset_config || 'front_left')
        .split('_')
        .map(part => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ');
    const countText =
        headMode.subset_type === 'row' ? `, ${headMode.row_count || 1} row${Number(headMode.row_count || 1) === 1 ? '' : 's'}` :
        headMode.subset_type === 'column' ? `, ${headMode.column_count || 1} column${Number(headMode.column_count || 1) === 1 ? '' : 's'}` :
        headMode.subset_type === 'rectangle' ? `, ${headMode.row_count || 1}x${headMode.column_count || 1}` :
        '';
    return `${subsetLabels[headMode.subset_type] || headMode.subset_type} (${orientation}${countText})`;
}

function selectedHeadCells(headType, headMode) {
    const geometry = getHeadGeometry(headType);
    const normalized = normalizeHeadModeForUi(headType, headMode);
    const front = normalized.subset_config.startsWith('front');
    const left = normalized.subset_config.endsWith('left');
    const rowStart = normalized.row_count >= geometry.rows ? 0 : (front ? 0 : geometry.rows - normalized.row_count);
    const colStart = normalized.column_count >= geometry.columns ? 0 : (left ? 0 : geometry.columns - normalized.column_count);
    const selected = new Set();
    for (let row = rowStart; row < rowStart + normalized.row_count; row++) {
        for (let col = colStart; col < colStart + normalized.column_count; col++) {
            selected.add(`${row}:${col}`);
        }
    }
    return { geometry, normalized, selected, rowStart, colStart };
}

function headModeAnchorCell(geometry, normalized, rowStart, colStart) {
    const front = normalized.subset_config.startsWith('front');
    const left = normalized.subset_config.endsWith('left');
    return {
        row: front ? rowStart : (rowStart + normalized.row_count - 1),
        col: left ? colStart : (colStart + normalized.column_count - 1),
    };
}

function displayRowToModelRow(geometry, displayRow) {
    return geometry.rows - 1 - displayRow;
}

function getTipboxGeometry(detail) {
    const wells = Number(detail?.wells || 0);
    const rows = Number(detail?.rows || (wells === 96 ? 8 : (wells === 384 ? 16 : (wells === 1536 ? 32 : 0))));
    const cols = Number(detail?.cols || (wells === 96 ? 12 : (wells === 384 ? 24 : (wells === 1536 ? 48 : 0))));
    if (!rows || !cols) return null;
    const spacingX = Number(detail?.spacing_x_mm || 0);
    const spacingY = Number(detail?.spacing_y_mm || 0);
    const inferredPitchX = cols >= 48 ? 2.25 : (cols >= 24 ? 4.5 : 9.0);
    const inferredPitchY = rows >= 32 ? 2.25 : (rows >= 16 ? 4.5 : 9.0);
    const heightMm = Math.max(
        Number(detail?.height_mm || detail?.height || 0),
        Number(detail?.stack_height_mm || 0),
        20.0,
    );
    return {
        rows,
        cols,
        pitchX: spacingX > 0 ? spacingX : inferredPitchX,
        pitchY: spacingY > 0 ? spacingY : inferredPitchY,
        heightMm,
    };
}

function selectedTipboxCells(detail, headMode, tipSelection) {
    const geometry = getTipboxGeometry(detail);
    if (!geometry || !tipSelection) return { selected: new Set(), anchorKey: null };
    const normalized = normalizeHeadModeForUi(state.headType || 'HT_96_D_70', headMode);
    const rowCount = Math.max(1, Number(tipSelection.row_count || normalized.row_count || 1));
    const columnCount = Math.max(1, Number(tipSelection.column_count || normalized.column_count || 1));
    const rowStart = Math.max(0, Math.min(geometry.rows - rowCount, Number(tipSelection.row || 0)));
    const colStart = Math.max(0, Math.min(geometry.cols - columnCount, Number(tipSelection.col || 0)));
    const selected = new Set();
    for (let row = rowStart; row < rowStart + rowCount; row++) {
        for (let col = colStart; col < colStart + columnCount; col++) {
            selected.add(`${row}:${col}`);
        }
    }
    const headAnchor = String(tipSelection.head_anchor || tipSelection.mirror_corner || normalized.subset_config || 'back_left');
    const anchorRow = headAnchor.startsWith('front')
        ? rowStart + rowCount - 1
        : rowStart;
    const anchorCol = headAnchor.endsWith('right')
        ? colStart + columnCount - 1
        : colStart;
    return { selected, anchorKey: `${anchorRow}:${anchorCol}` };
}

function getTipboxInventory(location) {
    return state.tipboxInventory?.[String(location)] || null;
}

function getTransientTipTaskState() {
    const status = state.taskStatus || {};
    if (!['tips_on', 'tips_off'].includes(status.task)) return null;
    return status;
}

function getVisibleTipboxOccupancy(location, detail) {
    const geometry = getTipboxGeometry(detail);
    const inventory = getTipboxInventory(location);
    const occupied = new Set(inventory?.occupied || []);
    const task = getTransientTipTaskState();
    if (!geometry || !task || Number(task.location) !== Number(location) || !task.tip_selection) {
        return occupied;
    }
    const transient = selectedTipboxCells(detail, task.head_mode || state.headMode, task.tip_selection);
    if (task.task === 'tips_on' && task.transfer_stage === 'mounted') {
        for (const key of transient.selected) occupied.delete(key);
    }
    if (task.task === 'tips_off' && task.transfer_stage === 'returned') {
        for (const key of transient.selected) occupied.add(key);
    }
    return occupied;
}

function getSelectionAnchorKey(selection) {
    if (!selection) return null;
    const anchorRow = Number(selection.anchor_row);
    const anchorCol = Number(selection.anchor_col);
    const row = Number(selection.row || 0);
    const col = Number(selection.col || 0);
    return `${Number.isFinite(anchorRow) ? anchorRow : row}:${Number.isFinite(anchorCol) ? anchorCol : col}`;
}

function getLegalAnchorKeys(location) {
    const inventory = getTipboxInventory(location);
    if (!inventory) return new Set();
    const anchors = state.tipsOnHead ? inventory.legal_return_anchors : inventory.legal_pickup_anchors;
    return new Set((anchors || []).map(anchor => getSelectionAnchorKey(anchor)).filter(Boolean));
}

function getLegalSelectionFootprint(detail, location, headMode) {
    const inventory = getTipboxInventory(location);
    if (!inventory) {
        return { legalCells: new Set(), cellToAnchor: new Map() };
    }
    const anchors = state.tipsOnHead ? inventory.legal_return_anchors : inventory.legal_pickup_anchors;
    const legalCells = new Set();
    const cellToAnchor = new Map();
    for (const anchor of (anchors || [])) {
        const selection = selectedTipboxCells(
            detail,
            headMode,
            anchor,
        );
        const anchorSelection = {
            location,
            row: anchor.row,
            col: anchor.col,
            row_count: anchor.row_count,
            column_count: anchor.column_count,
            mirror_corner: anchor.mirror_corner,
        };
        for (const key of selection.selected) {
            legalCells.add(key);
            if (!cellToAnchor.has(key)) {
                cellToAnchor.set(key, anchorSelection);
            }
        }
    }
    return { legalCells, cellToAnchor };
}

function getRenderedHeadTipState() {
    const task = getTransientTipTaskState();
    if (task?.task === 'tips_on' && task.transfer_stage === 'mounted') {
        return {
            visible: true,
            headMode: task.head_mode || state.headMode,
            tipSelection: task.tip_selection || state.tipSelection,
            tipLabwareName: getDeckDetail(Number(task.location))?.name || state.tipLabware,
            attachedTipLengthMm: state.attachedTipLengthMm,
        };
    }
    if (task?.task === 'tips_off') {
        if (task.transfer_stage === 'mounted') {
            return {
                visible: true,
                headMode: task.head_mode || state.tipsOnHeadMode || state.headMode,
                tipSelection: state.tipsOnHeadSelection,
                tipLabwareName: state.tipLabware,
                attachedTipLengthMm: state.attachedTipLengthMm,
            };
        }
        return { visible: false };
    }
    return {
        visible: state.tipsOnHead,
        headMode: state.tipsOnHeadMode || state.headMode,
        tipSelection: state.tipsOnHeadSelection,
        tipLabwareName: state.tipLabware,
        attachedTipLengthMm: state.attachedTipLengthMm,
    };
}

function findTipLabwareDetail(name) {
    if (!name) return null;
    for (const items of Object.values(state.deckDetails || {})) {
        if (!Array.isArray(items)) continue;
        for (const item of items) {
            if (item?.name === name) return item;
        }
    }
    for (const item of (state.labwareCatalog || [])) {
        if (!item) continue;
        if (item.name === name || item.id === name || item.display_name === name) {
            return item.detail || item.metadata || item;
        }
    }
    return null;
}

function inferTipCapacityFromLength(lengthMm) {
    const length = Number(lengthMm || 0);
    if (length <= 0) return 10;
    if (length <= 30) return 10;
    if (length <= 40) return 30;
    return 200;
}

function getHeadTipPitchMm(headType) {
    const geometry = getHeadGeometry(headType);
    if (geometry.columns >= 48 || geometry.rows >= 32) return { pitchX: 2.25, pitchY: 2.25 };
    if (geometry.columns >= 24 || geometry.rows >= 16) return { pitchX: 4.5, pitchY: 4.5 };
    return { pitchX: 9.0, pitchY: 9.0 };
}

function getHeadTipHostLink() {
    if (!urdfRobot?._links) return null;
    return (
        urdfRobot._links['384_head_384_head']
        || urdfRobot._links['384_head']
        || urdfRobot._links.zaxis
        || urdfRobot._links.head
        || urdfRobot._links.tool
        || urdfRobot._links.gripperzaxis
        || urdfRobot.group
    );
}

function getHeadTipMountFrame(hostLink) {
    if (!hostLink) {
        return { centerX: 0, centerY: 0, minZ: -0.01 };
    }
    hostLink.updateWorldMatrix(true, true);
    const inverse = new THREE.Matrix4().copy(hostLink.matrixWorld).invert();
    const localBox = new THREE.Box3();
    const samplePoints = [];
    let hasMesh = false;
    hostLink.traverse((obj) => {
        if (!obj.isMesh || !obj.geometry) return;
        if (!obj.geometry.boundingBox) obj.geometry.computeBoundingBox();
        if (!obj.geometry.boundingBox) return;
        const box = obj.geometry.boundingBox.clone();
        const matrix = new THREE.Matrix4().multiplyMatrices(inverse, obj.matrixWorld);
        box.applyMatrix4(matrix);
        localBox.union(box);
        hasMesh = true;
        const positions = obj.geometry.getAttribute?.('position');
        if (!positions) return;
        const point = new THREE.Vector3();
        for (let i = 0; i < positions.count; i++) {
            point.fromBufferAttribute(positions, i).applyMatrix4(matrix);
            samplePoints.push({ x: point.x, y: point.y, z: point.z });
        }
    });
    if (!hasMesh) {
        return { centerX: 0, centerY: 0, minZ: -0.01 };
    }
    const minZ = localBox.min.z;
    const sliceThickness = 0.012;
    const bottomSlice = samplePoints.filter((point) => point.z <= (minZ + sliceThickness));
    const footprint = bottomSlice.length ? bottomSlice : samplePoints;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const point of footprint) {
        if (point.x < minX) minX = point.x;
        if (point.x > maxX) maxX = point.x;
        if (point.y < minY) minY = point.y;
        if (point.y > maxY) maxY = point.y;
    }
    return {
        centerX: Number.isFinite(minX) && Number.isFinite(maxX) ? (minX + maxX) / 2 : 0,
        centerY: Number.isFinite(minY) && Number.isFinite(maxY) ? (minY + maxY) / 2 : 0,
        minZ,
    };
}

// ── Three.js Setup ─────────────────────────────────────────────────────

const viewport = document.getElementById('viewport');
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.2;
viewport.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = hexToThreeColor(getComputedStyle(document.documentElement).getPropertyValue('--bg-viewport'), 0x0d0d14);
const stlLoader = new STLLoader();
const gltfLoader = new GLTFLoader();
const raycaster = new THREE.Raycaster();
const pointerNdc = new THREE.Vector2();
const labwareRoot = new THREE.Group();
labwareRoot.name = 'labware-root';
const accessoryRoot = new THREE.Group();
accessoryRoot.name = 'accessory-root';
const headTipsRoot = new THREE.Group();
headTipsRoot.name = 'head-tips-root';
const deckSlotAnchors = new Map();
const deckSlotReplacementAnchors = new Map();
const deckSlotLinkNames = new Map();
const deckSlotPadMeshes = new Map();
const deckLabwareMeshes = new Map();
const labwareTemplateCache = new Map();
const accessoryTemplateCache = new Map();
const tipTemplateCache = new Map();
let labwareRefreshToken = 0;
let accessoryRefreshToken = 0;
let accessoryVisualRefreshHandle = null;
const accessorySurfaceOffsetsM = new Map();
const carryAnimation = {
    active: false,
    sourceLoc: null,
    offset: null,
};

const camera = new THREE.PerspectiveCamera(45, 1, 0.001, 100);
camera.position.set(0.6, 0.5, 0.6);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.3, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.update();

const ambientLight = new THREE.AmbientLight(0xffffff, 1.25);
scene.add(ambientLight);

const dirLight = new THREE.DirectionalLight(0xffffff, 0.55);
dirLight.position.set(1, 2, 1.5);
scene.add(dirLight);

const fillLight = new THREE.DirectionalLight(0xffffff, 0.35);
fillLight.position.set(-1, 0.5, -1);
scene.add(fillLight);


// ── Bravo Coordinate System Gizmo ──────────────────────────────────────
// URDF (onshape-to-robot) uses Z-up; the gizmo labels match Bravo's
// motion axes so the operator can correlate jog directions with the 3-D view.

const gizmoSize = 140;
const gizmoRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
gizmoRenderer.setPixelRatio(window.devicePixelRatio);
gizmoRenderer.setSize(gizmoSize, gizmoSize);
gizmoRenderer.setClearColor(0x000000, 0);
Object.assign(gizmoRenderer.domElement.style, {
    position: 'absolute', bottom: '12px', left: '12px',
    pointerEvents: 'none', borderRadius: '8px',
    background: getComputedStyle(document.documentElement).getPropertyValue('--bg-gizmo').trim() || 'rgba(18, 18, 26, 0.7)',
    border: `1px solid ${getComputedStyle(document.documentElement).getPropertyValue('--gizmo-border').trim() || '#2a2a3a'}`,
});
viewport.appendChild(gizmoRenderer.domElement);

const gizmoScene = new THREE.Scene();
const gizmoCamera = new THREE.PerspectiveCamera(50, 1, 0.1, 10);
gizmoCamera.position.set(0, 0, 3);

function makeArrow(dir, color, label, sublabel) {
    const group = new THREE.Group();
    const shaftGeo = new THREE.CylinderGeometry(0.03, 0.03, 0.8, 8);
    const shaftMat = new THREE.MeshBasicMaterial({ color });
    const shaft = new THREE.Mesh(shaftGeo, shaftMat);
    shaft.position.copy(dir.clone().multiplyScalar(0.4));
    shaft.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
    group.add(shaft);

    const coneGeo = new THREE.ConeGeometry(0.08, 0.2, 8);
    const coneMat = new THREE.MeshBasicMaterial({ color });
    const cone = new THREE.Mesh(coneGeo, coneMat);
    cone.position.copy(dir.clone().multiplyScalar(0.9));
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
    group.add(cone);

    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = `#${color.toString(16).padStart(6, '0')}`;
    ctx.font = 'bold 32px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(label, 64, 28);
    ctx.font = '18px sans-serif';
    ctx.fillStyle = '#aaaaaa';
    ctx.fillText(sublabel, 64, 52);

    const tex = new THREE.CanvasTexture(canvas);
    const spriteMat = new THREE.SpriteMaterial({ map: tex, depthTest: false });
    const sprite = new THREE.Sprite(spriteMat);
    sprite.scale.set(0.6, 0.3, 1);
    sprite.position.copy(dir.clone().multiplyScalar(1.25));
    group.add(sprite);
    return group;
}

const gizmoGroup = new THREE.Group();
gizmoGroup.add(makeArrow(new THREE.Vector3(1, 0, 0), 0xff4444, '+X', 'Right'));
gizmoGroup.add(makeArrow(new THREE.Vector3(0, 0, 1), 0x44cc44, '+Y', 'Forward'));
gizmoGroup.add(makeArrow(new THREE.Vector3(0, -1, 0), 0x4488ff, '+Z', 'Down'));

const originGeo = new THREE.SphereGeometry(0.06, 12, 12);
const originMat = new THREE.MeshBasicMaterial({ color: 0x888888 });
gizmoGroup.add(new THREE.Mesh(originGeo, originMat));
gizmoScene.add(gizmoGroup);

// ── URDF Model Loading ────────────────────────────────────────────────
// Joint → Bravo axis mapping.
// homeOffset (mm): Bravo position when URDF joint value = 0.
// scale: sign correction for axes where the URDF joint moves opposite to the Bravo axis.
//   zaxis upper=0, lower=-0.105 → head moves in the negative direction as Z increases.
const JOINT_AXIS_MAP = {
    'xaxis':          { bravoAxis: 'X',  homeOffset: 193.04, scale:  1 },
    'yaxis':          { bravoAxis: 'Y',  homeOffset: 0,      scale:  1 },
    'zaxis':          { bravoAxis: 'Z',  homeOffset: 0,      scale: -1 },
    // The exported URDF puts the gripper Z carriage under ygantry instead of
    // under the main Z carriage, so the viewer has to synthesize the hidden Z
    // contribution explicitly: the gripper should follow Z and then apply its
    // own relative Zg travel on top of that. DARWIN's nested/resting Zg is
    // about -20 mm, so that offset should render as zero extra travel.
    'zaxis-gripper':  {
        bravoAxis: 'Zg',
        homeOffset: -20,
        scale: 1,
        coupledAxis: 'Z',
        coupledScale: 1,
    },
    // Both fingers get half the G distance. Their opposite rpy values mean the
    // same positive joint value opens one finger left and the other right.
    'ygripper-left':  { bravoAxis: 'G',  homeOffset: 0,      scale:  0.5 },
    'ygripper-right': { bravoAxis: 'G',  homeOffset: 0,      scale:  0.5 },
};

let urdfRobot = null;

// ── Camera view state ─────────────────────────────────────────────────
// Populated once the URDF model is loaded.
let modelCenter  = new THREE.Vector3();
let modelSize    = new THREE.Vector3();
let isoPosition  = new THREE.Vector3(0.6, 0.5, 0.6); // updated after load

// Smooth camera fly-to: lerp position + target over ~400 ms.
const camAnim = {
    active:    false,
    startMs:   0,
    durMs:     400,          // milliseconds
    fromPos:   new THREE.Vector3(),
    toPos:     new THREE.Vector3(),
    fromLook:  new THREE.Vector3(),
    toLook:    new THREE.Vector3(),
};

function goToView(preset) {
    if (!modelCenter || modelSize.length() === 0) return;
    const c = modelCenter;
    const d = Math.max(modelSize.x, modelSize.y, modelSize.z) * 1.6;

    const views = {
        iso:    new THREE.Vector3(c.x + modelSize.x * 1.2, c.y + modelSize.y * 0.8, c.z + modelSize.z * 1.2),
        front:  new THREE.Vector3(c.x,      c.y,      c.z - d),  // -Z (looking forward)
        back:   new THREE.Vector3(c.x,      c.y,      c.z + d),  // +Z (looking from behind)
        right:  new THREE.Vector3(c.x + d,  c.y,      c.z),
        left:   new THREE.Vector3(c.x - d,  c.y,      c.z),
        top:    new THREE.Vector3(c.x,      c.y + d,  c.z),
        bottom: new THREE.Vector3(c.x,      c.y - d,  c.z),
    };

    const targetPos = views[preset];
    if (!targetPos) return;

    camAnim.fromPos.copy(camera.position);
    camAnim.toPos.copy(targetPos);
    camAnim.fromLook.copy(controls.target);
    camAnim.toLook.copy(c);
    camAnim.startMs = performance.now();
    camAnim.active  = true;
}

// Minimal URDF robot model with per-joint position control.
class URDFModel {
    constructor() {
        this.group = new THREE.Group();
        this._links = {};
        this._joints = {}; // name → { child, originPos, axisInParent }
    }

    // value is in metres (URDF joint space).
    setJointValue(name, valueM) {
        const j = this._joints[name];
        if (!j) return;
        j.child.position.copy(j.originPos).addScaledVector(j.axisInParent, valueM);
    }
}

function parseVec3(str) {
    if (!str) return [0, 0, 0];
    return str.trim().split(/\s+/).map(parseFloat);
}

function isDeckPositionVisual(name) {
    return /^hw1_300_001(?:_\d+)?_hw1_300_001(?:_\d+)?$/i.test(name);
}

function deckLocationFromLinkName(name) {
    const match = /^hw1_300_001(?:_(\d+))?_hw1_300_001(?:_\d+)?$/i.exec(name);
    if (!match) return null;
    return Number(match[1] || '1');
}

function assignDeckPositionColors(deckVisuals) {
    deckSlotLinkNames.clear();
    deckSlotPadMeshes.clear();
    deckVisuals.forEach((deckVisual) => {
        const location = deckLocationFromLinkName(deckVisual.linkName);
        if (!location) return;
        const grey = DECK_POSITION_GREYS[location - 1];
        if (grey === undefined) return;
        deckVisual.mesh.material.color.setHex(grey);
        deckVisual.mesh.material.roughness = 0.75;
        deckVisual.mesh.material.metalness = 0.05;
        deckSlotLinkNames.set(location, deckVisual.linkName);
        if (!deckSlotPadMeshes.has(location)) deckSlotPadMeshes.set(location, []);
        deckSlotPadMeshes.get(location).push(deckVisual.mesh);
    });
}

function resolveLabwareModelUrl(modelPath) {
    if (!modelPath) return null;
    if (/^(https?:)?\/\//i.test(modelPath) || modelPath.startsWith('/')) {
        return `${modelPath}${modelPath.includes('?') ? '&' : '?'}v=labware3`;
    }
    const encoded = modelPath.split('/').map(part => encodeURIComponent(part)).join('/');
    return `/labware/${encoded}?v=labware3`;
}

function resolveStaticModelUrl(modelPath, cacheKey) {
    if (!modelPath) return null;
    if (/^(https?:)?\/\//i.test(modelPath) || modelPath.startsWith('/')) {
        return `${modelPath}${modelPath.includes('?') ? '&' : '?'}v=${cacheKey}`;
    }
    const encoded = modelPath.split('/').map(part => encodeURIComponent(part)).join('/');
    return `/static/${encoded}?v=${cacheKey}`;
}

function accessoryModelPath(device) {
    const configured = String(device?.model?.path || '').trim();
    if (configured) return configured;
    return device?.type === 'teleshake' ? DEFAULT_TELESHAKE_MODEL_PATH : '';
}

function resolveAccessoryModelUrl(device) {
    return resolveStaticModelUrl(accessoryModelPath(device), 'accessory6');
}

function enabledAccessoriesAtLocation(location) {
    return state.accessoryDevices.filter(item => (
        item.enabled
        && Number(item.location || 0) === Number(location)
    ));
}

function updateDeckPadVisibility() {
    for (const [loc, meshes] of deckSlotPadMeshes.entries()) {
        const replacesPad = enabledAccessoriesAtLocation(loc).some(item => (
            item.type === 'teleshake' || Boolean(accessoryModelPath(item))
        ));
        for (const mesh of meshes) {
            mesh.visible = !replacesPad;
        }
    }
}

function getDeckDetail(location) {
    const items = state.deckDetails[String(location)];
    return Array.isArray(items) && items.length ? items[items.length - 1] : null;
}

function getDeckStackDetails(location) {
    const items = state.deckDetails[String(location)];
    return Array.isArray(items) ? items : [];
}

function getTeachpoint(location) {
    return state.teachpoints?.[String(location)] || state.teachpoints?.[location] || null;
}

function syncProcessLabwareSelection() {
    const procLocation = document.getElementById('proc-location');
    const procLabware = document.getElementById('proc-labware');
    if (!procLocation || !procLabware) return;

    const location = parseInt(procLocation.value || '0', 10);
    const detail = location ? getDeckDetail(location) : null;
    const definitionId = String(detail?.definition_id || detail?.id || '');

    if (definitionId) {
        procLabware.value = definitionId;
        if (procLabware.value !== definitionId) {
            const option = Array.from(procLabware.options).find((item) => item.value === definitionId);
            if (!option) {
                procLabware.add(new Option(detail?.name || definitionId, definitionId));
                procLabware.value = definitionId;
            }
        }
    } else {
        procLabware.value = '';
    }

    renderProcess2DSelector();
    updateProcessPlateHandlingLabels();
}

// ── Log panel sizing ────────────────────────────────────────────────────────
// The log is the third row of the #app grid. Its height is a CSS variable so it
// can be dragged, and collapsing just pins that variable to the header height.
// Both survive a reload, because re-sizing the log every session is precisely
// the kind of small friction that makes a tool tiring to use.

const LOG_HEIGHT_KEY = 'openbravo.logHeight';
const LOG_COLLAPSED_KEY = 'openbravo.logCollapsed';
const LOG_MIN_PX = 60;

// Collapsed height is measured, not guessed: it is the header row plus the
// panel's own vertical padding, which depends on the active media query.
function logCollapsedPx() {
    const panel = document.getElementById('bottom-panel');
    const header = panel?.querySelector('.panel-header');
    if (!panel || !header) return 32;
    const style = getComputedStyle(panel);
    const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    return Math.ceil(header.getBoundingClientRect().height + padding);
}

function logMaxPx() {
    return Math.max(LOG_MIN_PX, Math.round(window.innerHeight * 0.75));
}

function readStoredNumber(key) {
    try {
        const raw = parseFloat(localStorage.getItem(key));
        return Number.isFinite(raw) ? raw : null;
    } catch {
        return null;
    }
}

function storeValue(key, value) {
    try {
        localStorage.setItem(key, String(value));
    } catch {
        // Private browsing or a full quota — the session still works.
    }
}

// Only the floor is applied here. The 75vh ceiling lives in the grid rule so
// that a short window clamps what is drawn without overwriting the stored
// preference — otherwise shrinking the window once would shrink the log forever.
function applyLogHeight(px) {
    state.logHeightPx = Math.max(LOG_MIN_PX, Math.round(px));
    document.getElementById('app')?.style.setProperty('--log-height', `${state.logHeightPx}px`);
    return state.logHeightPx;
}

function setLogCollapsed(collapsed, persist = true) {
    const panel = document.getElementById('bottom-panel');
    const btn = document.getElementById('btn-log-toggle');
    state.logCollapsed = Boolean(collapsed);
    panel?.classList.toggle('collapsed', state.logCollapsed);

    if (state.logCollapsed) {
        document.getElementById('app')?.style.setProperty('--log-height', `${logCollapsedPx()}px`);
    } else {
        applyLogHeight(state.logHeightPx || readStoredNumber(LOG_HEIGHT_KEY) || 120);
    }
    if (btn) {
        btn.innerHTML = state.logCollapsed ? '&plus;' : '&minus;';
        btn.title = state.logCollapsed ? 'Restore the log' : 'Minimize the log';
        btn.setAttribute('aria-label', btn.title);
        btn.setAttribute('aria-expanded', state.logCollapsed ? 'false' : 'true');
    }
    if (persist) storeValue(LOG_COLLAPSED_KEY, state.logCollapsed ? '1' : '0');
}

function initLogPanel() {
    const stored = readStoredNumber(LOG_HEIGHT_KEY);
    state.logHeightPx = stored || 120;
    let collapsed = false;
    try {
        collapsed = localStorage.getItem(LOG_COLLAPSED_KEY) === '1';
    } catch { /* default to expanded */ }
    setLogCollapsed(collapsed, false);

    const handle = document.getElementById('log-resize');
    if (!handle) return;

    const onDown = (event) => {
        event.preventDefault();
        const startY = event.clientY ?? event.touches?.[0]?.clientY;
        if (startY == null) return;
        const startHeight = document.getElementById('bottom-panel').offsetHeight;
        handle.classList.add('dragging');
        document.body.classList.add('log-resizing');

        const onMove = (moveEvent) => {
            const y = moveEvent.clientY ?? moveEvent.touches?.[0]?.clientY;
            if (y == null) return;
            // Dragging up grows the log, which is the direction that matches
            // the handle sitting on its top edge.
            const next = applyLogHeight(Math.min(logMaxPx(), startHeight + (startY - y)));
            if (state.logCollapsed && next > logCollapsedPx()) setLogCollapsed(false);
        };
        const onUp = () => {
            handle.classList.remove('dragging');
            document.body.classList.remove('log-resizing');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            window.removeEventListener('touchmove', onMove);
            window.removeEventListener('touchend', onUp);
            if (!state.logCollapsed) storeValue(LOG_HEIGHT_KEY, state.logHeightPx);
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
        window.addEventListener('touchmove', onMove, { passive: false });
        window.addEventListener('touchend', onUp);
    };

    handle.addEventListener('mousedown', onDown);
    handle.addEventListener('touchstart', onDown, { passive: false });
    // Double-clicking the grab strip is the usual shortcut for collapse.
    handle.addEventListener('dblclick', () => setLogCollapsed(!state.logCollapsed));

    document.getElementById('btn-log-toggle')?.addEventListener('click', () => {
        setLogCollapsed(!state.logCollapsed);
    });
}

initLogPanel();

function getStoredTheme() {
    try {
        const value = localStorage.getItem(THEME_STORAGE_KEY);
        return value === 'light' || value === 'dark' ? value : 'dark';
    } catch {
        return 'dark';
    }
}

function setStoredTheme(theme) {
    try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
        // Ignore storage failures and keep the active in-memory theme.
    }
}

function syncThemeToggleLabel() {
    const button = document.getElementById('btn-theme-toggle');
    if (button) {
        button.textContent = `Theme: ${state.theme === 'light' ? 'Light' : 'Dark'}`;
        button.title = 'Toggle light and dark mode';
    }
}

function hexToThreeColor(cssValue, fallback) {
    if (typeof cssValue !== 'string' || !cssValue.trim()) {
        return new THREE.Color(fallback);
    }
    return new THREE.Color(cssValue.trim());
}

function applyTheme(theme, persist = true) {
    state.theme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = state.theme;
    document.body.dataset.theme = state.theme;
    syncThemeToggleLabel();
    if (persist) {
        setStoredTheme(state.theme);
    }
    if (typeof scene !== 'undefined' && scene) {
        const styles = getComputedStyle(document.documentElement);
        scene.background = hexToThreeColor(styles.getPropertyValue('--bg-viewport'), 0x0d0d14);
        if (gizmoRenderer?.domElement) {
            gizmoRenderer.domElement.style.background = styles.getPropertyValue('--bg-gizmo').trim() || 'rgba(18, 18, 26, 0.7)';
            gizmoRenderer.domElement.style.border = `1px solid ${styles.getPropertyValue('--gizmo-border').trim() || '#2a2a3a'}`;
        }
    }
}

applyTheme(getStoredTheme(), false);

function describeDeckLocationLabware(location) {
    const detail = getDeckDetail(Number(location || 0));
    return detail?.name || 'Empty';
}

function updateProcessPlateHandlingLabels() {
    const baseLocation = parseInt(document.getElementById('stack-base-location')?.value || '0', 10);
    const sourceLocation = parseInt(document.getElementById('stack-source-location')?.value || '0', 10);
    const destackSourceLocation = parseInt(document.getElementById('destack-source-location')?.value || '0', 10);
    const destackDestinationLocation = parseInt(document.getElementById('destack-destination-location')?.value || '0', 10);
    const mountBaseLocation = parseInt(document.getElementById('mount-base-location')?.value || '0', 10);
    const mountSourceLocation = parseInt(document.getElementById('mount-source-location')?.value || '0', 10);
    const unmountSourceLocation = parseInt(document.getElementById('unmount-source-location')?.value || '0', 10);
    const unmountDestinationLocation = parseInt(document.getElementById('unmount-destination-location')?.value || '0', 10);
    const plateLocation = parseInt(document.getElementById('delid-plate-location')?.value || '0', 10);
    const lidDestination = parseInt(document.getElementById('delid-lid-destination')?.value || '0', 10);
    const relidLidLocation = parseInt(document.getElementById('relid-lid-location')?.value || '0', 10);
    const relidPlateLocation = parseInt(document.getElementById('relid-plate-location')?.value || '0', 10);
    const procLocation = parseInt(document.getElementById('proc-location')?.value || '0', 10);
    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    setText('stack-base-plate', describeDeckLocationLabware(baseLocation));
    setText('stack-source-plate', describeDeckLocationLabware(sourceLocation));
    setText('destack-source-plate', describeDeckLocationLabware(destackSourceLocation));
    setText('destack-destination-name', describeDeckLocationLabware(destackDestinationLocation));
    setText('mount-base-plate', describeDeckLocationLabware(mountBaseLocation));
    setText('mount-source-plate', describeDeckLocationLabware(mountSourceLocation));
    setText('unmount-source-plate', describeDeckLocationLabware(unmountSourceLocation));
    setText('unmount-destination-name', describeDeckLocationLabware(unmountDestinationLocation));
    setText('delid-plate-name', describeDeckLocationLabware(plateLocation));
    setText('delid-destination-name', describeDeckLocationLabware(lidDestination));
    setText('relid-lid-name', describeDeckLocationLabware(relidLidLocation));
    setText('relid-plate-name', describeDeckLocationLabware(relidPlateLocation));
    const procDetail = procLocation ? getDeckDetail(procLocation) : null;
    const stackThickness = Number(procDetail?.stack_height_mm || procDetail?.stack_height || procDetail?.height_mm || procDetail?.height || 0);
    setText('scan-stack-plate-name', describeDeckLocationLabware(procLocation));
    setText('scan-stack-thickness', stackThickness > 0 ? `${stackThickness.toFixed(1)} mm` : '-');
}

function updateScanStackResultFields(result = null) {
    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    if (!result) {
        setText('scan-stack-measured', '-');
        setText('scan-stack-count', '-');
        setText('scan-stack-rounded', '-');
        return;
    }
    setText('scan-stack-measured', result.measured_height_mm != null ? `${Number(result.measured_height_mm).toFixed(1)} mm` : '-');
    setText('scan-stack-count', result.inferred_count != null ? String(result.inferred_count) : '-');
    setText('scan-stack-rounded', result.rounded_stack_height_mm != null ? `${Number(result.rounded_stack_height_mm).toFixed(0)} mm` : '-');
}

function sanitizeProcessNumericValue(value) {
    if (!value) return '';
    let sanitized = value.replace(/[^\d.]/g, '');
    const dotIndex = sanitized.indexOf('.');
    if (dotIndex >= 0) {
        sanitized = sanitized.slice(0, dotIndex + 1) + sanitized.slice(dotIndex + 1).replace(/\./g, '');
    }
    return sanitized;
}

function formatProcessNumericValue(el) {
    const sanitized = sanitizeProcessNumericValue(el.value);
    if (!sanitized) {
        el.value = '';
        return;
    }
    let numeric = Number(sanitized);
    if (Number.isNaN(numeric)) {
        el.value = '';
        return;
    }
    const minAttr = el.getAttribute('min');
    if (minAttr) {
        const minValue = Number(minAttr);
        if (!Number.isNaN(minValue) && numeric < minValue) {
            numeric = minValue;
        }
    }
    const stepAttr = el.getAttribute('step');
    const decimals = stepAttr && stepAttr.includes('.') ? stepAttr.split('.')[1].length : 0;
    el.value = decimals ? numeric.toFixed(decimals) : String(Math.round(numeric));
}

function bindProcessNumericInputs() {
    document.querySelectorAll('.process-numeric').forEach((input) => {
        input.addEventListener('input', () => {
            const cleaned = sanitizeProcessNumericValue(input.value);
            if (input.value !== cleaned) input.value = cleaned;
        });
        input.addEventListener('blur', () => formatProcessNumericValue(input));
    });
}

function getLabwareGridGeometry(detail) {
    if (!detail) return null;
    const rows = Number(detail.rows || 0);
    const cols = Number(detail.cols || 0);
    if (rows > 0 && cols > 0) return { rows, cols };
    const wells = Number(detail.wells || 0);
    if (wells === 96) return { rows: 8, cols: 12 };
    if (wells === 384) return { rows: 16, cols: 24 };
    if (wells === 1536) return { rows: 32, cols: 48 };
    if (wells === 24) return { rows: 4, cols: 6 };
    if (wells === 48) return { rows: 6, cols: 8 };
    if (wells === 6) return { rows: 2, cols: 3 };
    return null;
}

function getProcessSelectorSelectionKey(selection) {
    if (!selection) return null;
    return [
        Number(selection.row || 0),
        Number(selection.col || 0),
        Number(selection.row_count || 1),
        Number(selection.column_count || 1),
        String(selection.mirror_corner || ''),
    ].join(':');
}

function getCurrentProcessSelection() {
    const procLocation = document.getElementById('proc-location');
    const location = parseInt(procLocation?.value || '0', 10);
    if (!location) return null;
    if (state.tipSelection && Number(state.tipSelection.location) === location) {
        return state.tipSelection;
    }
    return null;
}

function getCurrentProcessPlateSelection(location) {
    return state.processWellSelection?.[String(location)] || null;
}

function getPlateLegalitySignature(location, detail, geometry) {
    return JSON.stringify({
        location,
        definition: detail?.definition_id || detail?.id || detail?.name || '',
        rows: geometry?.rows || 0,
        cols: geometry?.cols || 0,
        headMode: state.headMode || null,
    });
}

function getLegalPlateAnchorSet(location) {
    const anchors = state.processWellLegalAnchors?.[String(location)] || [];
    return new Set(anchors.map((anchor) => `${Number(anchor.row || 0)}:${Number(anchor.col || 0)}`));
}

async function syncProcessPlateSelectionState(location, detail, geometry) {
    if (!location || !detail || !geometry) return;
    const key = String(location);
    const signature = getPlateLegalitySignature(location, detail, geometry);
    if (state.processWellLegalitySignatures?.[key] === signature || state.processWellLegalityLoading?.[key]) {
        return;
    }
    state.processWellLegalityLoading[key] = true;
    const res = await apiCall('/api/plate_selection', 'GET', { location });
    state.processWellLegalityLoading[key] = false;
    if (!res) return;
    state.processWellSelection = state.processWellSelection || {};
    state.processWellLegalAnchors = state.processWellLegalAnchors || {};
    state.processWellLegalitySignatures = state.processWellLegalitySignatures || {};
    if (res.selection) {
        state.processWellSelection[key] = {
            row: Number(res.selection.row || 0),
            col: Number(res.selection.col || 0),
        };
    }
    state.processWellLegalAnchors[key] = Array.isArray(res.legal_anchors) ? res.legal_anchors : [];
    state.processWellFootprint = state.processWellFootprint || {};
    state.processWellFootprint[key] = Array.isArray(res.footprint) ? res.footprint : [];
    state.processWellLegalitySignatures[key] = signature;
    renderProcess2DSelector();
}

async function handleProcessTipSelection(selection) {
    const res = await apiCall('/api/tip_selection', 'PUT', selection);
    if (!res?.tip_selection) return;
    state.tipSelection = res.tip_selection;
    const rowCount = Math.max(1, Number(res.tip_selection.row_count || selection.row_count || 1));
    const columnCount = Math.max(1, Number(res.tip_selection.column_count || selection.column_count || 1));
    const rowEnd = Number(res.tip_selection.row || selection.row || 0) + rowCount;
    const colEnd = Number(res.tip_selection.col || selection.col || 0) + columnCount;
    log(`Selected tip region rows ${Number(res.tip_selection.row || selection.row || 0) + 1}-${rowEnd}, columns ${Number(res.tip_selection.col || selection.col || 0) + 1}-${colEnd}`, 'info');
    renderProcess2DSelector();
    void refreshDeckLabwareScene();
}

function getProcessSelectorMetrics(geometry, isTipbox) {
    if (isTipbox) {
        return { trackSize: 8, gap: 4, wellSize: 9 };
    }
    const totalWells = Number(geometry?.rows || 0) * Number(geometry?.cols || 0);
    if (totalWells >= 1536) {
        return { trackSize: 7, gap: 2, wellSize: 7 };
    }
    if (totalWells >= 384) {
        return { trackSize: 8, gap: 3, wellSize: 8 };
    }
    return { trackSize: 8, gap: 4, wellSize: 9 };
}

function renderProcess2DSelector() {
    const titleEl = document.getElementById('proc-2d-title');
    const statusEl = document.getElementById('proc-2d-status');
    const gridEl = document.getElementById('proc-2d-grid');
    const legendEl = document.getElementById('proc-2d-legend');
    const noteEl = document.getElementById('proc-2d-note');
    const procLocation = document.getElementById('proc-location');
    if (!titleEl || !statusEl || !gridEl || !legendEl || !noteEl || !procLocation) return;

    const location = parseInt(procLocation.value || '0', 10);
    const detail = location ? getDeckDetail(location) : null;
    const geometry = getLabwareGridGeometry(detail);
    const isTipbox = detail && ['tip_box', 'tip_trash'].includes(String(detail.base_class || detail.kind || '').toLowerCase());

    gridEl.innerHTML = '';
    legendEl.innerHTML = '';

    if (!detail || !geometry) {
        titleEl.textContent = detail?.name || 'No Labware Selected';
        statusEl.textContent = 'Select a configured location.';
        noteEl.textContent = 'Tipboxes support live 2D selection now. Plate well selection is rendered here so aspirate, dispense, and mix can use the same surface next.';
        return;
    }

    titleEl.textContent = detail.name || detail.definition_id || `Location ${location}`;
    const metrics = getProcessSelectorMetrics(geometry, isTipbox);
    gridEl.style.gridTemplateColumns = `repeat(${geometry.cols}, ${metrics.trackSize}px)`;
    gridEl.style.setProperty('--proc-grid-gap', `${metrics.gap}px`);
    gridEl.style.setProperty('--proc-well-size', `${metrics.wellSize}px`);

    if (isTipbox) {
        const inventory = getTipboxInventory(location);
        const visibleOccupancy = getVisibleTipboxOccupancy(location, detail);
        const { legalCells, cellToAnchor } = getLegalSelectionFootprint(detail, location, state.tipsOnHead ? (state.tipsOnHeadMode || state.headMode) : state.headMode);
        const currentSelection = getCurrentProcessSelection();
        const currentSelectionCells = selectedTipboxCells(detail, state.tipsOnHead ? (state.tipsOnHeadMode || state.headMode) : state.headMode, currentSelection);
        const currentSelectionKey = getProcessSelectorSelectionKey(currentSelection);

        statusEl.textContent = state.tipsOnHead
            ? 'Click a legal return region to choose where the current tip pattern should go.'
            : 'Click a legal pickup region to choose which tips to mount.';
        noteEl.textContent = inventory
            ? `Showing ${state.tipsOnHead ? 'return' : 'pickup'} legality from the live backend tipbox inventory.`
            : 'No live tipbox inventory is available for this location.';
        legendEl.innerHTML = `
            <span><i class="dot-legal"></i>legal region</span>
            <span><i class="dot-anchor"></i>anchor</span>
            <span><i class="dot-occupied"></i>tip present</span>
            <span><i class="dot-empty"></i>empty</span>
        `;

        for (let displayRow = 0; displayRow < geometry.rows; displayRow++) {
            for (let col = 0; col < geometry.cols; col++) {
                const row = displayRow;
                const key = `${row}:${col}`;
                const cell = document.createElement('button');
                cell.type = 'button';
                cell.className = 'process-selector-cell';
                cell.title = `Row ${row + 1}, Column ${col + 1}`;
                cell.classList.add(visibleOccupancy.has(key) ? 'tip-occupied' : 'tip-empty');
                if (legalCells.has(key)) cell.classList.add('legal');
                if (currentSelectionCells.selected.has(key)) cell.classList.add('selected');
                if (currentSelectionCells.anchorKey === key) cell.classList.add('anchor');
                const mappedSelection = cellToAnchor.get(key);
                if (!mappedSelection) cell.disabled = true;
                cell.addEventListener('click', () => {
                    if (!mappedSelection) return;
                    const mappedKey = getProcessSelectorSelectionKey(mappedSelection);
                    if (mappedKey === currentSelectionKey) return;
                    void handleProcessTipSelection(mappedSelection);
                });
                gridEl.appendChild(cell);
            }
        }
        return;
    }

    statusEl.textContent = `${geometry.rows} row${geometry.rows === 1 ? '' : 's'} × ${geometry.cols} column${geometry.cols === 1 ? '' : 's'} layout.`;
    noteEl.textContent = 'Legal anchor wells come from the backend and are revalidated during aspirate, dispense, and mix.';
    legendEl.innerHTML = `<span><i class="dot-legal"></i>legal anchor</span><span><i class="dot-anchor"></i>selected anchor</span><span><i class="dot-footprint"></i>active wells</span><span><i class="dot-well"></i>blocked</span>`;
    const legalityKey = String(location);
    const legalitySignature = getPlateLegalitySignature(location, detail, geometry);
    const legalAnchors = getLegalPlateAnchorSet(location);
    const selectedWell = getCurrentProcessPlateSelection(location);
    const footprintSet = new Set(
        (state.processWellFootprint?.[String(location)] || []).map(w => `${w.row}:${w.col}`)
    );
    if (state.processWellLegalitySignatures?.[legalityKey] !== legalitySignature) {
        statusEl.textContent = 'Loading legal anchor wells...';
        void syncProcessPlateSelectionState(location, detail, geometry);
    }
    for (let row = 0; row < geometry.rows; row++) {
        for (let col = 0; col < geometry.cols; col++) {
            const cell = document.createElement('button');
            cell.type = 'button';
            cell.className = 'process-selector-cell well';
            cell.title = `Row ${row + 1}, Column ${col + 1}`;
            const anchorKey = `${row}:${col}`;
            if (legalAnchors.has(anchorKey)) {
                cell.classList.add('legal');
            } else if (!footprintSet.has(anchorKey)) {
                cell.disabled = true;
            }
            if (footprintSet.has(anchorKey)) {
                cell.classList.add('footprint');
            }
            if (selectedWell && Number(selectedWell.row) === row && Number(selectedWell.col) === col) {
                cell.classList.add('selected');
            }
            cell.addEventListener('click', async () => {
                if (!legalAnchors.has(anchorKey)) return;
                const res = await apiCall('/api/plate_selection', 'PUT', { location, row, col });
                if (!res?.plate_selection) return;
                state.processWellSelection = state.processWellSelection || {};
                state.processWellSelection[String(location)] = {
                    row: Number(res.plate_selection.row || row),
                    col: Number(res.plate_selection.col || col),
                };
                if (Array.isArray(res.legal_anchors)) {
                    state.processWellLegalAnchors = state.processWellLegalAnchors || {};
                    state.processWellLegalAnchors[String(location)] = res.legal_anchors;
                }
                state.processWellFootprint = state.processWellFootprint || {};
                state.processWellFootprint[String(location)] = Array.isArray(res.footprint) ? res.footprint : [];
                const rowLabel = String.fromCharCode(65 + row);
                log(`Selected legal anchor well ${rowLabel}${col + 1}`, 'info');
                renderProcess2DSelector();
            });
            gridEl.appendChild(cell);
        }
    }
}

function updateVisionUiVisibility() {
    const visible = Boolean(state.visionEnabled);
    const controlsRow = document.getElementById('vision-controls-row');
    if (controlsRow) controlsRow.style.display = visible ? 'flex' : 'none';
    const startBtn = document.getElementById('btn-start-vision-service');
    if (startBtn) startBtn.style.display = visible ? '' : 'none';
    const topLink = document.getElementById('btn-open-vision-calibration');
    if (topLink) topLink.style.display = visible ? '' : 'none';
    const inlineLink = document.getElementById('btn-open-vision-calibration-inline');
    if (inlineLink) inlineLink.style.display = visible ? '' : 'none';
}

function fitLinear(samples) {
    if (samples.length < 2) return null;
    const n = samples.length;
    let sumX = 0;
    let sumY = 0;
    let sumXX = 0;
    let sumXY = 0;
    for (const sample of samples) {
        sumX += sample.x;
        sumY += sample.y;
        sumXX += sample.x * sample.x;
        sumXY += sample.x * sample.y;
    }
    const denom = n * sumXX - sumX * sumX;
    if (Math.abs(denom) < 1e-9) return null;
    const scale = (n * sumXY - sumX * sumY) / denom;
    const offset = (sumY - scale * sumX) / n;
    return { scale, offset };
}

function updateDeckMotionMapping() {
    const xSamples = [];
    const ySamples = [];
    for (let loc = 1; loc <= 9; loc++) {
        const anchor = deckSlotAnchors.get(loc);
        const tp = getTeachpoint(loc);
        if (!anchor || !tp) continue;
        if (typeof tp.x === 'number') xSamples.push({ x: tp.x, y: anchor.x });
        if (typeof tp.y === 'number') ySamples.push({ x: tp.y, y: anchor.y });
    }
    const xFit = fitLinear(xSamples);
    const yFit = fitLinear(ySamples);
    state.deckMotionMap = xFit && yFit ? { x: xFit, y: yFit } : null;
}

function buildDeckSlotAnchors(linkGroups) {
    deckSlotAnchors.clear();
    deckSlotReplacementAnchors.clear();
    for (const [loc, linkName] of deckSlotLinkNames.entries()) {
        const group = linkGroups[linkName];
        if (!group) continue;
        const box = new THREE.Box3().setFromObject(group);
        const center = box.getCenter(new THREE.Vector3());
        deckSlotReplacementAnchors.set(
            loc,
            new THREE.Vector3(
                center.x,
                center.y,
                box.min.z,
            ),
        );
        deckSlotAnchors.set(
            loc,
            new THREE.Vector3(
                center.x,
                center.y,
                box.max.z - DECK_SLOT_SURFACE_OFFSET_M,
            ),
        );
    }
    updateDeckMotionMapping();
}

function mapRobotXYToDeckLocal(xMm, yMm) {
    if (!state.deckMotionMap) return null;
    return new THREE.Vector3(
        state.deckMotionMap.x.scale * xMm + state.deckMotionMap.x.offset,
        state.deckMotionMap.y.scale * yMm + state.deckMotionMap.y.offset,
        0,
    );
}

function getDetailWellDimensions(detail) {
    const wellDimensions = detail?.well_dimensions_mm;
    return wellDimensions && typeof wellDimensions === 'object' ? wellDimensions : (detail || {});
}

function getLabwareCenterOffsetFromTeachpointMm(detail) {
    const wellDimensions = getDetailWellDimensions(detail);
    const lengthMm = Math.max(0.0, Number(detail?.length_mm || detail?.length || 0.0));
    const widthMm = Math.max(0.0, Number(detail?.width_mm || detail?.width || 0.0));
    const offsetXmm = Number(wellDimensions?.offset_x_mm ?? detail?.offset_x_mm ?? 0.0);
    const offsetYmm = Number(wellDimensions?.offset_y_mm ?? detail?.offset_y_mm ?? 0.0);
    return {
        x: (lengthMm / 2.0) - offsetXmm,
        y: (widthMm / 2.0) - offsetYmm,
    };
}

function getLabwarePlacementAnchor(location, detail, fallbackAnchor) {
    return fallbackAnchor.clone();
}

function median(values) {
    if (!values.length) return 1;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
}

function normalizeLabwareModel(model, detail) {
    model.rotation.x = Math.PI / 2;
    const targetSize = [
        Math.max(0.001, Number(detail.length_mm || detail.length || 0) / 1000),
        Math.max(0.001, Number(detail.width_mm || detail.width || 0) / 1000),
        Math.max(0.001, Number(detail.height_mm || detail.height || 0) / 1000),
    ];
    const initialBox = new THREE.Box3().setFromObject(model);
    const initialSize = initialBox.getSize(new THREE.Vector3());
    const sourceSize = [initialSize.x, initialSize.y, initialSize.z].filter(v => v > 1e-6);
    if (sourceSize.length === 3) {
        const scale = median(
            sourceSize
                .slice()
                .sort((a, b) => a - b)
                .map((value, index) => targetSize.slice().sort((a, b) => a - b)[index] / value),
        );
        if (Number.isFinite(scale) && scale > 0) {
            model.scale.setScalar(scale);
        }
    }
    const finalBox = new THREE.Box3().setFromObject(model);
    const center = finalBox.getCenter(new THREE.Vector3());
    model.position.set(-center.x, -center.y, -finalBox.min.z);
    const appearanceOverride = LABWARE_APPEARANCE_OVERRIDES[detail?.name || ''] || null;
    model.traverse(obj => {
        if (!obj.isMesh) return;
        if (appearanceOverride) {
            obj.material = new THREE.MeshStandardMaterial({
                color: appearanceOverride.color,
                transparent: appearanceOverride.transparent,
                opacity: appearanceOverride.opacity,
                roughness: appearanceOverride.roughness,
                metalness: appearanceOverride.metalness,
            });
            obj.renderOrder = 2;
        } else {
            const baseMaterial = obj.material?.clone?.() || obj.material;
            obj.material = baseMaterial;
        }
        obj.castShadow = true;
        obj.receiveShadow = true;
    });
}

async function buildLabwareMesh(detail) {
    if (String(detail?.render_mode || '').toLowerCase() === 'generated_lid'
        || String(detail?.base_class || '').toLowerCase() === 'lid'
        || String(detail?.kind || '').toLowerCase() === 'lid') {
        return buildGeneratedLidMesh(detail);
    }
    const baseDetail = detail?.generated_lid
        ? {
            ...detail,
            height_mm: Number(detail?.base_height_mm || detail?.height_mm || detail?.height || 14.4),
        }
        : detail;
    const url = resolveLabwareModelUrl(detail?.model_3d);
    if (!url) return attachGeneratedLidMesh(buildFallbackLabwareMesh(baseDetail), detail);
    const cacheKey = `${url}|${baseDetail.length_mm || 0}|${baseDetail.width_mm || 0}|${baseDetail.height_mm || baseDetail.height || 0}`;
    if (!labwareTemplateCache.has(cacheKey)) {
        labwareTemplateCache.set(cacheKey, new Promise((resolve) => {
            gltfLoader.load(url, (gltf) => {
                const wrapper = new THREE.Group();
                const source = gltf.scene || gltf.scenes?.[0];
                if (!source) {
                    resolve(buildFallbackLabwareMesh(baseDetail));
                    return;
                }
                const clone = SkeletonUtils.clone(source);
                wrapper.add(clone);
                normalizeLabwareModel(clone, baseDetail);
                resolve(wrapper);
            }, undefined, () => resolve(buildFallbackLabwareMesh(baseDetail)));
        }));
    }
    const template = await labwareTemplateCache.get(cacheKey);
    return template ? attachGeneratedLidMesh(template.clone(true), detail) : null;
}

function accessoryDeckYaw(device) {
    return device?.type === 'teleshake' ? Math.PI / 2 : 0;
}

function teleshakeMaterialForPart(partBox, modelBox) {
    const size = partBox.getSize(new THREE.Vector3());
    const center = partBox.getCenter(new THREE.Vector3());
    const modelSize = modelBox.getSize(new THREE.Vector3());
    const height = Math.max(0.001, modelSize.z);
    const footprint = Math.max(0, size.x * size.y);
    const modelFootprint = Math.max(0.001, modelSize.x * modelSize.y);
    const topRatio = (center.z - modelBox.min.z) / height;
    const zMinRatio = (partBox.min.z - modelBox.min.z) / height;
    const nearCorner = (
        Math.abs(center.x) > modelSize.x * 0.24
        && Math.abs(center.y) > modelSize.y * 0.13
    );
    if (nearCorner && zMinRatio > 0.20) return TELESHAKE_MATERIALS.blue;
    if (nearCorner && topRatio > 0.14) return TELESHAKE_MATERIALS.side;
    if (footprint > modelFootprint * 0.25 && topRatio > 0.55) return TELESHAKE_MATERIALS.top;
    if (footprint > modelFootprint * 0.25 && topRatio < 0.25) return TELESHAKE_MATERIALS.base;
    if (topRatio < 0.18) return TELESHAKE_MATERIALS.base;
    if (topRatio > 0.35) return TELESHAKE_MATERIALS.top;
    return TELESHAKE_MATERIALS.side;
}

function applyAccessoryAppearance(model, device, modelBox) {
    model.updateWorldMatrix(true, true);
    model.traverse(obj => {
        if (!obj.isMesh) return;
        if (device?.type === 'teleshake') {
            const partBox = new THREE.Box3().setFromObject(obj);
            obj.material = teleshakeMaterialForPart(partBox, modelBox);
        } else {
            obj.material = obj.material?.clone?.() || new THREE.MeshStandardMaterial({
                color: 0xd7dde4,
                roughness: 0.72,
                metalness: 0.08,
            });
        }
        obj.castShadow = true;
        obj.receiveShadow = true;
    });
}

function normalizeAccessoryModel(model, device) {
    model.rotation.x = Math.PI / 2;
    const initialBox = new THREE.Box3().setFromObject(model);
    const center = initialBox.getCenter(new THREE.Vector3());
    model.position.set(-center.x, -center.y, -initialBox.min.z);
    model.updateWorldMatrix(true, true);
    const finalBox = new THREE.Box3().setFromObject(model);
    applyAccessoryAppearance(model, device, finalBox);
    return Math.max(0, finalBox.getSize(new THREE.Vector3()).z);
}

function buildFallbackAccessoryMesh(device) {
    if (device?.type !== 'teleshake') return null;
    const group = new THREE.Group();
    const base = new THREE.Mesh(
        new THREE.BoxGeometry(0.14, 0.10, 0.028),
        TELESHAKE_MATERIALS.base,
    );
    base.position.z = 0.014;
    base.castShadow = true;
    base.receiveShadow = true;
    group.add(base);

    const top = new THREE.Mesh(
        new THREE.BoxGeometry(0.11, 0.08, 0.004),
        TELESHAKE_MATERIALS.top,
    );
    top.position.z = 0.030;
    top.castShadow = true;
    top.receiveShadow = true;
    group.add(top);

    for (const x of [-0.052, 0.052]) {
        for (const y of [-0.032, 0.032]) {
            const cap = new THREE.Mesh(
                new THREE.BoxGeometry(0.026, 0.018, 0.006),
                TELESHAKE_MATERIALS.blue,
            );
            cap.position.set(x, y, 0.036);
            cap.castShadow = true;
            cap.receiveShadow = true;
            group.add(cap);
        }
    }
    group.userData.accessoryHeightM = 0.039;
    group.rotation.z = accessoryDeckYaw(device);
    return group;
}

async function buildAccessoryMesh(device) {
    const url = resolveAccessoryModelUrl(device);
    if (!url) return null;
    const cacheKey = `${url}|${device?.type || 'accessory'}`;
    if (!accessoryTemplateCache.has(cacheKey)) {
        accessoryTemplateCache.set(cacheKey, new Promise((resolve) => {
            gltfLoader.load(url, (gltf) => {
                const wrapper = new THREE.Group();
                const source = gltf.scene || gltf.scenes?.[0];
                if (!source) {
                    resolve(buildFallbackAccessoryMesh(device));
                    return;
                }
                const clone = SkeletonUtils.clone(source);
                wrapper.add(clone);
                wrapper.userData.accessoryHeightM = normalizeAccessoryModel(clone, device);
                wrapper.rotation.z = accessoryDeckYaw(device);
                resolve(wrapper);
            }, undefined, () => resolve(buildFallbackAccessoryMesh(device)));
        }));
    }
    const template = await accessoryTemplateCache.get(cacheKey);
    return template ? template.clone(true) : null;
}

async function refreshAccessoryScene() {
    const token = ++accessoryRefreshToken;
    updateDeckPadVisibility();
    if (!urdfRobot || deckSlotAnchors.size === 0) {
        accessoryRoot.clear();
        accessorySurfaceOffsetsM.clear();
        return;
    }

    const nextMeshes = [];
    const nextSurfaceOffsets = new Map();
    for (const device of state.accessoryDevices) {
        if (!device.enabled) continue;
        const loc = Number(device.location || 0);
        const anchor = deckSlotAnchors.get(loc);
        const replacementAnchor = deckSlotReplacementAnchors.get(loc) || anchor;
        if (!loc || !anchor || !replacementAnchor) continue;
        const mesh = await buildAccessoryMesh(device);
        if (!mesh) continue;
        mesh.name = `accessory-${device.id || device.type || loc}`;
        mesh.position.copy(replacementAnchor);
        nextMeshes.push(mesh);
        const heightM = Math.max(0, Number(mesh.userData?.accessoryHeightM || 0));
        const labwareLiftM = Math.max(0, replacementAnchor.z + heightM - anchor.z);
        nextSurfaceOffsets.set(loc, Math.max(nextSurfaceOffsets.get(loc) || 0, labwareLiftM));
    }

    if (token !== accessoryRefreshToken) return;
    accessoryRoot.clear();
    for (const mesh of nextMeshes) accessoryRoot.add(mesh);
    accessorySurfaceOffsetsM.clear();
    for (const [loc, heightM] of nextSurfaceOffsets.entries()) {
        accessorySurfaceOffsetsM.set(loc, heightM);
    }
    updateDeckPadVisibility();
}

function buildFallbackLabwareMesh(detail) {
    const lengthM = Math.max(0.01, Number(detail?.length_mm || detail?.length || 127.76) / 1000);
    const widthM = Math.max(0.01, Number(detail?.width_mm || detail?.width || 85.48) / 1000);
    const heightM = Math.max(
        0.006,
        Number(detail?.height_mm || detail?.height || detail?.stack_height_mm || 14.4) / 1000,
    );
    const color = (() => {
        const baseClass = String(detail?.base_class || '').toLowerCase();
        const kind = String(detail?.kind || '').toLowerCase();
        if (baseClass === 'tip_box' || kind === 'tip_box') return 0xcfd3da;
        if (baseClass === 'tip_trash' || kind === 'tip_trash') return 0xa6adb8;
        return 0xe4e6eb;
    })();
    const geometry = new THREE.BoxGeometry(lengthM, widthM, heightM);
    const material = new THREE.MeshStandardMaterial({
        color,
        roughness: 0.82,
        metalness: 0.03,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.position.z = heightM / 2;

    const wrapper = new THREE.Group();
    wrapper.add(mesh);
    return wrapper;
}

function buildGeneratedLidMesh(detail, options = {}) {
    const lengthM = Math.max(0.01, Number(detail?.length_mm || detail?.length || 127.76) / 1000);
    const widthM = Math.max(0.01, Number(detail?.width_mm || detail?.width || 85.48) / 1000);
    const heightM = Math.max(0.001, Number(detail?.height_mm || detail?.lid_thickness_mm || 2.0) / 1000);
    const skirtHeightM = Math.max(0.0007, Math.min(heightM * 0.65, heightM - 0.0005));
    const topThicknessM = Math.max(0.0005, heightM - skirtHeightM);
    const skirtThicknessM = Math.max(0.0009, Math.min(lengthM, widthM) * 0.035);
    const group = new THREE.Group();
    const material = new THREE.MeshStandardMaterial({
        color: options.attached ? 0xdde4ee : 0xe8edf4,
        transparent: true,
        opacity: options.attached ? 0.72 : 0.82,
        roughness: 0.28,
        metalness: 0.02,
    });
    const top = new THREE.Mesh(new THREE.BoxGeometry(lengthM, widthM, topThicknessM), material.clone());
    top.position.z = heightM - (topThicknessM / 2);
    top.castShadow = true;
    top.receiveShadow = true;
    group.add(top);

    const wallGeometryX = new THREE.BoxGeometry(lengthM, skirtThicknessM, skirtHeightM);
    const wallGeometryY = new THREE.BoxGeometry(skirtThicknessM, widthM, skirtHeightM);
    const halfLength = lengthM / 2;
    const halfWidth = widthM / 2;
    const wallZ = skirtHeightM / 2;
    const walls = [
        [wallGeometryX, 0, halfWidth - (skirtThicknessM / 2), wallZ],
        [wallGeometryX, 0, -halfWidth + (skirtThicknessM / 2), wallZ],
        [wallGeometryY, halfLength - (skirtThicknessM / 2), 0, wallZ],
        [wallGeometryY, -halfLength + (skirtThicknessM / 2), 0, wallZ],
    ];
    walls.forEach(([geometry, x, y, z]) => {
        const wall = new THREE.Mesh(geometry, material.clone());
        wall.position.set(x, y, z);
        wall.castShadow = true;
        wall.receiveShadow = true;
        group.add(wall);
    });
    return group;
}

function attachGeneratedLidMesh(wrapper, detail) {
    const lidDetail = detail?.generated_lid;
    if (!wrapper || !lidDetail) return wrapper;
    const lid = buildGeneratedLidMesh(lidDetail, { attached: true });
    const baseHeightM = Math.max(0.001, Number(detail?.base_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000);
    const totalHeightM = Math.max(baseHeightM, Number(detail?.total_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000);
    const lidHeightM = Math.max(0.001, Number(lidDetail?.height_mm || lidDetail?.lid_thickness_mm || 1.0) / 1000);
    lid.position.z = Math.max(0, totalHeightM - lidHeightM);
    wrapper.add(lid);
    return wrapper;
}

function tipModelUrlForDetail(detail) {
    const headType = state.headType || 'HT_96_D_70';
    const byTipId = getTipDefinitionForSelection(headType, detail?.tip_definition_id);
    if (byTipId?.model_3d) return byTipId.model_3d;
    const capacity = Number(detail?.disposable_tip_capacity_ul || 0);
    if (capacity > 0 && capacity <= 10) return '/labware-assets/tips/d10.gltf?v=tips2';
    if (capacity > 10 && capacity <= 30) return '/labware-assets/tips/d30.gltf?v=tips2';
    return null;
}

async function buildTipTemplate(detail) {
    const url = tipModelUrlForDetail(detail);
    if (!url) return null;
    const cacheKey = `${url}|${detail.disposable_tip_capacity_ul || 0}`;
    if (!tipTemplateCache.has(cacheKey)) {
        tipTemplateCache.set(cacheKey, new Promise((resolve) => {
            gltfLoader.load(url, (gltf) => {
                const wrapper = new THREE.Group();
                const source = gltf.scene || gltf.scenes?.[0];
                if (!source) {
                    resolve(null);
                    return;
                }
                const clone = SkeletonUtils.clone(source);
                clone.rotation.x = Math.PI / 2;
                const bbox = new THREE.Box3().setFromObject(clone);
                const size = bbox.getSize(new THREE.Vector3());
                const explicitLengthMm = Number(detail?.tip_length_mm || detail?.attached_tip_length_mm || 0);
                const targetLengthM = Math.max(
                    0.008,
                    (explicitLengthMm > 0
                        ? explicitLengthMm
                        : (getTipHeightForCapacity(state.headType || 'HT_96_D_70', detail.disposable_tip_capacity_ul) || 20.0)) / 1000,
                );
                const sourceLength = Math.max(size.x, size.y, size.z, 1e-6);
                const scale = targetLengthM / sourceLength;
                clone.scale.setScalar(scale);
                const finalBox = new THREE.Box3().setFromObject(clone);
                const center = finalBox.getCenter(new THREE.Vector3());
                clone.position.set(-center.x, -center.y, -finalBox.min.z);
                clone.traverse(obj => {
                    if (!obj.isMesh) return;
                    obj.material = new THREE.MeshStandardMaterial({
                        color: 0xd9d9de,
                        roughness: 0.85,
                        metalness: 0.05,
                    });
                    obj.castShadow = true;
                    obj.receiveShadow = true;
                });
                wrapper.add(clone);
                wrapper.userData.tipHeightM = targetLengthM;
                resolve(wrapper);
            }, undefined, () => resolve(null));
        }));
    }
    const template = await tipTemplateCache.get(cacheKey);
    return template ? template.clone(true) : null;
}

async function attachTipboxTips(entry, location) {
    const detail = entry.detail;
    const geometry = getTipboxGeometry(detail);
    const isTipBox = ['tip_box'].includes(String(detail?.base_class || detail?.kind || '').toLowerCase());
    if (!geometry || !isTipBox) return;
    const tipTemplate = await buildTipTemplate(detail);
    if (!tipTemplate) return;
    const tipHeightM = Number(tipTemplate.userData?.tipHeightM || 0.02);
    const tipsGroup = new THREE.Group();
    tipsGroup.name = `tipbox-tips-${location}`;
    const { selected, anchorKey } = selectedTipboxCells(
        detail,
        state.headMode,
        state.tipSelection && Number(state.tipSelection.location) === Number(location) ? state.tipSelection : null,
    );
    const mountedFromHere =
        state.tipsOnHead
        && state.tipLabware
        && state.tipLabware === detail?.name
        && state.tipsOnHeadSelection
        && Number(state.tipsOnHeadSelection.location) === Number(location);
    const mountedSelection = mountedFromHere
        ? selectedTipboxCells(detail, state.tipsOnHeadMode || state.headMode, state.tipsOnHeadSelection)
        : { selected: new Set(), anchorKey: null };
    const occupied = getVisibleTipboxOccupancy(location, detail);
    const legalAnchorKeys = getLegalAnchorKeys(location);
    const { legalCells, cellToAnchor } = getLegalSelectionFootprint(detail, location, state.tipsOnHead ? (state.tipsOnHeadMode || state.headMode) : state.headMode);
    const xOrigin = -((geometry.cols - 1) * geometry.pitchX) / 2;
    const yOrigin = -((geometry.rows - 1) * geometry.pitchY) / 2;
    const zOffset = Math.max(0, (geometry.heightMm / 1000) - tipHeightM);
    for (let row = 0; row < geometry.rows; row++) {
        for (let col = 0; col < geometry.cols; col++) {
            const key = `${row}:${col}`;
            if (mountedSelection.selected.has(key) || !occupied.has(key)) continue;
            const tip = tipTemplate.clone(true);
            const displayRow = geometry.rows - 1 - row;
            tip.position.set(
                (xOrigin + col * geometry.pitchX) / 1000,
                (yOrigin + displayRow * geometry.pitchY) / 1000,
                zOffset,
            );
            const legalAnchor = legalAnchorKeys.has(key);
            const legalCell = legalCells.has(key);
            const mappedSelection = cellToAnchor.get(key);
            if (legalAnchor) {
                tip.userData.tipSelection = mappedSelection || { location, row, col };
            } else if (mappedSelection) {
                tip.userData.tipSelection = mappedSelection;
            }
            tip.traverse(obj => {
                if (!obj.isMesh) return;
                if (legalAnchor) obj.userData.tipSelection = mappedSelection || { location, row, col };
                else if (mappedSelection) obj.userData.tipSelection = mappedSelection;
                const selectedCell = selected.has(key);
                const anchorCell = key === anchorKey;
                if (obj.material?.color) {
                    obj.material = obj.material.clone();
                    obj.material.transparent = !legalCell;
                    obj.material.opacity = legalCell ? 1.0 : 0.2;
                    obj.material.color.setHex(anchorCell ? 0xff5f5f : (selectedCell ? 0xc43d3d : (legalCell ? 0x59c77d : 0xd9d9de)));
                    if (selectedCell || legalCell) {
                        obj.material.emissive = new THREE.Color(anchorCell ? 0x661111 : 0x330909);
                    }
                }
            });
            tipsGroup.add(tip);
        }
    }
    entry.group.add(tipsGroup);
}

async function refreshHeadTipScene() {
    headTipsRoot.clear();
    const renderedHeadState = getRenderedHeadTipState();
    if (!urdfRobot || !renderedHeadState?.visible) return;
    const hostLink = getHeadTipHostLink();
    if (!hostLink) return;
    if (headTipsRoot.parent !== hostLink) {
        headTipsRoot.removeFromParent();
        hostLink.add(headTipsRoot);
    }
    const tipLabwareDetail = findTipLabwareDetail(renderedHeadState.tipLabwareName) || {};
    const tipDetail = {
        ...tipLabwareDetail,
        disposable_tip_capacity_ul: Number(
            tipLabwareDetail.disposable_tip_capacity_ul || inferTipCapacityFromLength(renderedHeadState.attachedTipLengthMm),
        ),
        attached_tip_length_mm: Number(renderedHeadState.attachedTipLengthMm || tipLabwareDetail.tip_length_mm || 0),
        tip_length_mm: Number(renderedHeadState.attachedTipLengthMm || tipLabwareDetail.tip_length_mm || 0),
    };
    const tipTemplate = await buildTipTemplate(tipDetail);
    if (!tipTemplate) return;
    const headType = state.headType || 'HT_96_D_70';
    const selection = selectedHeadCells(headType, renderedHeadState.headMode || state.headMode);
    const { pitchX, pitchY } = getHeadTipPitchMm(headType);
    const xOrigin = -((selection.geometry.columns - 1) * pitchX) / 2;
    const yOrigin = -((selection.geometry.rows - 1) * pitchY) / 2;
    const mountFrame = getHeadTipMountFrame(hostLink);
    const tipHeightM = Number(
        tipTemplate.userData?.tipHeightM || Math.max(0.008, Number(renderedHeadState.attachedTipLengthMm || 20) / 1000),
    );
    for (let row = 0; row < selection.geometry.rows; row++) {
        for (let col = 0; col < selection.geometry.columns; col++) {
            if (!selection.selected.has(`${row}:${col}`)) continue;
            const tip = tipTemplate.clone(true);
            tip.position.set(
                mountFrame.centerX + ((xOrigin + col * pitchX) / 1000),
                mountFrame.centerY + ((yOrigin + row * pitchY) / 1000),
                mountFrame.minZ - tipHeightM + 0.002,
            );
            tip.traverse((obj) => {
                if (!obj.isMesh || !obj.material?.color) return;
                obj.material = obj.material.clone();
                obj.material.color.setHex(0xe8e8ed);
            });
            headTipsRoot.add(tip);
        }
    }
}

async function refreshDeckLabwareScene() {
    const token = ++labwareRefreshToken;
    if (!urdfRobot || deckSlotAnchors.size === 0) return;
    await refreshAccessoryScene();
    const nextEntries = [];
    for (let loc = 1; loc <= 9; loc++) {
        const details = getDeckStackDetails(loc);
        const anchor = deckSlotAnchors.get(loc);
        if (!details.length || !anchor) continue;
        let supportHeightM = accessorySurfaceOffsetsM.get(loc) || 0;
        for (let index = 0; index < details.length; index++) {
            const detail = details[index];
            const mesh = await buildLabwareMesh(detail);
            if (!mesh) continue;
            const entryAnchor = getLabwarePlacementAnchor(loc, detail, anchor);
            entryAnchor.z += supportHeightM;
            mesh.position.copy(entryAnchor);
            const entry = { group: mesh, anchor: entryAnchor.clone(), detail };
            await attachTipboxTips(entry, loc);
            nextEntries.push([loc, entry, index === details.length - 1]);
            supportHeightM += Math.max(
                0.001,
                Number(detail?.stack_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000,
            );
        }
    }
    if (token !== labwareRefreshToken) return;
    // Intentionally NOT calling resetCarryAnimation here. During a
    // pick_place the deck_details signature flickers at each step
    // transition, triggering this rebuild. If we reset the carry
    // every rebuild, updateLabwareAnimation re-arms with offset =
    // (anchor - gripper_NOW), which pins the plate back to its
    // source anchor every frame — the exact "plate teleports"
    // bug. The animate loop re-acquires the entry from the fresh
    // deckLabwareMeshes each frame via a plain .get(fromLoc), so
    // the new mesh instance gets driven by the existing offset.
    // If the source loc becomes empty (post-release), the
    // `!entry` early-return inside updateLabwareAnimation cleans
    // up the carry state itself.
    deckLabwareMeshes.clear();
    labwareRoot.clear();
    for (const [loc, entry, isTop] of nextEntries) {
        if (isTop) deckLabwareMeshes.set(loc, entry);
        labwareRoot.add(entry.group);
    }
    await refreshHeadTipScene();
}

function getCarriedLabwareHeight(detail) {
    return Math.max(0.005, Number(detail?.height_mm || detail?.height || 14.4) / 1000);
}

function resetCarryAnimation() {
    carryAnimation.active = false;
    carryAnimation.sourceLoc = null;
    carryAnimation.offset = null;
}

function getGripperCarryLocalPosition() {
    if (!urdfRobot?._links || !urdfRobot?.group) return null;
    const gripGroup =
        urdfRobot._links.gripperzaxis
        || urdfRobot._links.gripperzaxis_gripperzaxis
        || urdfRobot._links.fingerleft
        || urdfRobot._links.fingerright;
    if (!gripGroup) return null;
    const world = gripGroup.getWorldPosition(new THREE.Vector3());
    return urdfRobot.group.worldToLocal(world);
}

function isCarryAnimationStep(step) {
    return [
        'grip_plate',
        'grip_plate_complete',
        'move_to_carry_height',
        'move_to_carry_height_complete',
        'move_xy_to_place',
        'move_xy_to_place_complete',
        'move_to_place_height',
        'move_to_place_height_complete',
        'release_plate',
    ].includes(step);
}

function updateLabwareAnimation() {
    for (const entry of deckLabwareMeshes.values()) {
        entry.group.visible = true;
        entry.group.position.copy(entry.anchor);
    }

    const status = state.taskStatus || {};
    if (status.task !== 'pick_place' || !isCarryAnimationStep(status.step)) {
        resetCarryAnimation();
        return;
    }

    const fromLoc = Number(status.from_location || 0);
    const toLoc = Number(status.to_location || 0);
    const entry = deckLabwareMeshes.get(fromLoc);
    const destAnchor = deckSlotAnchors.get(toLoc);
    if (!entry || !destAnchor) {
        resetCarryAnimation();
        return;
    }

    const gripperLocal = getGripperCarryLocalPosition();
    const shouldFollowGripper = status.step !== 'move_to_place_height_complete' && status.step !== 'release_plate';
    if (shouldFollowGripper && gripperLocal) {
        if (!carryAnimation.active || carryAnimation.sourceLoc !== fromLoc || !carryAnimation.offset) {
            carryAnimation.active = true;
            carryAnimation.sourceLoc = fromLoc;
            carryAnimation.offset = entry.anchor.clone().sub(gripperLocal);
        }
        entry.group.position.copy(gripperLocal).add(carryAnimation.offset);
        return;
    }

    const xy = mapRobotXYToDeckLocal(state.renderPositions.X, state.renderPositions.Y);
    const carryHeight = Math.max(entry.anchor.z, destAnchor.z) + LABWARE_CARRY_CLEARANCE_M;
    const placeHeight = destAnchor.z + 0.001;
    const pos = xy ? new THREE.Vector3(xy.x, xy.y, carryHeight) : entry.anchor.clone();
    pos.x = destAnchor.x;
    pos.y = destAnchor.y;
    pos.z = placeHeight;
    entry.group.position.copy(pos);
}

async function loadURDF() {
    const URDF_URL  = '/model/pybravo_urdf/robot.urdf?v=deckfix4';
    const ASSET_BASE = '/model/pybravo_urdf/assets';

    log('Loading URDF model…', 'info');

    let xmlText;
    try {
        const resp = await fetch(URDF_URL);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        xmlText = await resp.text();
    } catch (e) {
        log(`Failed to fetch URDF: ${e.message}`, 'error');
        // Otherwise the badge is left reading "URDF loading…" indefinitely and
        // the empty viewport looks like a hang rather than a failed fetch.
        const statusEl = document.getElementById('urdf-status');
        if (statusEl) statusEl.textContent = `Robot model failed to load: ${e.message}`;
        return;
    }

    const xml = new DOMParser().parseFromString(xmlText, 'text/xml');
    const model = new URDFModel();
    deckSlotAnchors.clear();
    deckSlotReplacementAnchors.clear();

    // Create a Three.js Group for every <link>.
    const linkGroups = {};
    for (const el of xml.querySelectorAll('link')) {
        const name = el.getAttribute('name');
        if (name && !linkGroups[name]) {
            const g = new THREE.Group();
            g.name = name;
            linkGroups[name] = g;
        }
    }

    // Load visual STL meshes for each link.
    const meshPromises = [];
    const deckVisuals = [];
    for (const link of xml.querySelectorAll('link')) {
        const lname = link.getAttribute('name');
        const group = linkGroups[lname];
        if (!group) continue;

        for (const visual of link.querySelectorAll('visual')) {
            const meshEl = visual.querySelector('geometry mesh');
            if (!meshEl) continue;

            let filename = meshEl.getAttribute('filename') || '';
            // Resolve  package://packageName/relative/path.stl
            filename = filename.replace(/^package:\/\/([^/]*)\/(.*)$/, (_, _pkg, path) => {
                return `${ASSET_BASE}/${path}`;
            });

            const originEl = visual.querySelector('origin');
            const xyz = parseVec3(originEl?.getAttribute('xyz'));
            const rpy = parseVec3(originEl?.getAttribute('rpy'));

            const colorEl = visual.querySelector('material color');
            let color = new THREE.Color(0.82, 0.82, 0.84);
            const isFingerVisual =
                /(fingerleft|fingerright)\.stl$/i.test(filename) || /finger(left|right)/i.test(lname);
            const isHeadCarriageVisual =
                /^(384_head_384_head|gripperzaxis_gripperzaxis)$/i.test(lname) ||
                /(384_head|gripperzaxis)\.stl$/i.test(filename);
            const isDeckVisual = isDeckPositionVisual(lname);
            if (colorEl) {
                const rgba = colorEl.getAttribute('rgba').split(/\s+/).map(Number);
                color = new THREE.Color(rgba[0], rgba[1], rgba[2]);
            }
            if (isDeckVisual) {
                color = new THREE.Color(0xb9bbc5);
            } else if (isFingerVisual) {
                color = new THREE.Color(0xff7a00);
            } else if (isHeadCarriageVisual) {
                color = new THREE.Color(0xf2f2f2);
            }

            const promise = new Promise((resolve) => {
                stlLoader.load(filename, (geometry) => {
                    geometry.computeVertexNormals();
                    const mat = new THREE.MeshStandardMaterial({
                        color,
                        roughness: isDeckVisual ? 0.92 : 0.55,
                        metalness: isDeckVisual ? 0.0 : 0.25,
                    });
                    const mesh = new THREE.Mesh(geometry, mat);
                    mesh.castShadow = !isDeckVisual;
                    mesh.receiveShadow = true;
                    mesh.position.set(
                        xyz[0],
                        xyz[1] + (isHeadCarriageVisual ? HEAD_CARRIAGE_VISUAL_Y_OFFSET_M : 0),
                        xyz[2] + (isFingerVisual ? FINGER_VISUAL_Z_OFFSET_M : 0),
                    );
                    // URDF rpy = ZYX extrinsic (Rz·Ry·Rx); use Three.js 'ZYX' order.
                    mesh.quaternion.setFromEuler(new THREE.Euler(rpy[0], rpy[1], rpy[2], 'ZYX'));
                    group.add(mesh);
                    if (isDeckVisual) {
                        deckVisuals.push({ mesh, x: xyz[0], y: xyz[1], linkName: lname });
                    }
                    resolve();
                }, undefined, () => resolve());
            });
            meshPromises.push(promise);
        }
    }
    await Promise.all(meshPromises);
    assignDeckPositionColors(deckVisuals);

    // Wire up joints and build the kinematic tree.
    const childLinks = new Set();
    for (const joint of xml.querySelectorAll('joint')) {
        const jname = joint.getAttribute('name');
        const jtype = joint.getAttribute('type');
        const parentName = joint.querySelector('parent')?.getAttribute('link');
        const childName  = joint.querySelector('child')?.getAttribute('link');

        const parentGroup = linkGroups[parentName];
        const childGroup  = linkGroups[childName];
        if (!parentGroup || !childGroup) continue;

        const originEl = joint.querySelector('origin');
        const xyz = parseVec3(originEl?.getAttribute('xyz'));
        const rpy = parseVec3(originEl?.getAttribute('rpy'));
        if (childName === '384_head' || childName === 'gripperzaxis') {
            xyz[1] += TOOLING_ASSEMBLY_VISUAL_Y_OFFSET_M;
        }
        if (jname === 'ygripper-left' || jname === 'ygripper-right') {
            xyz[1] += FINGER_JOINT_Y_OFFSET_M;
        }

        childGroup.position.set(xyz[0], xyz[1], xyz[2]);
        // URDF rpy = ZYX extrinsic (Rz·Ry·Rx); use Three.js 'ZYX' order.
        childGroup.quaternion.setFromEuler(new THREE.Euler(rpy[0], rpy[1], rpy[2], 'ZYX'));
        parentGroup.add(childGroup);
        childLinks.add(childName);

        if (jtype === 'prismatic') {
            const axisEl = joint.querySelector('axis');
            const axisLocal = parseVec3(axisEl?.getAttribute('xyz') ?? '0 0 1');
            // Transform the joint-frame axis into the parent's frame using ZYX.
            const q = new THREE.Quaternion().setFromEuler(
                new THREE.Euler(rpy[0], rpy[1], rpy[2], 'ZYX'),
            );
            const axisInParent = new THREE.Vector3(...axisLocal).applyQuaternion(q);
            model._joints[jname] = {
                child: childGroup,
                originPos: new THREE.Vector3(xyz[0], xyz[1], xyz[2]),
                axisInParent,
            };
        }
    }

    // Root links (no joint makes them a child) attach directly to the model group.
    for (const [name, group] of Object.entries(linkGroups)) {
        if (!childLinks.has(name)) {
            model.group.add(group);
        }
    }
    model._links = linkGroups;
    buildDeckSlotAnchors(linkGroups);

    // URDF uses Z-up (ROS convention); Three.js uses Y-up.
    // Rotating -90° around X maps URDF +Z → Three.js +Y.
    model.group.rotation.x = -Math.PI / 2;
    labwareRoot.removeFromParent();
    labwareRoot.clear();
    accessoryRoot.removeFromParent();
    accessoryRoot.clear();
    model.group.add(labwareRoot);
    model.group.add(accessoryRoot);
    headTipsRoot.removeFromParent();
    headTipsRoot.clear();

    scene.add(model.group);
    urdfRobot = model;

    // Fit camera to the loaded model.
    const box  = new THREE.Box3().setFromObject(model.group);
    modelCenter = box.getCenter(new THREE.Vector3());
    modelSize   = box.getSize(new THREE.Vector3());
    controls.target.copy(modelCenter);
    const isoPos = new THREE.Vector3(
        modelCenter.x + modelSize.x * 1.2,
        modelCenter.y + modelSize.y * 0.8,
        modelCenter.z + modelSize.z * 1.2,
    );
    camera.position.copy(isoPos);
    isoPosition.copy(isoPos);
    controls.update();
    void refreshDeckLabwareScene();

    const n = Object.keys(model._joints).length;
    log(`URDF loaded (${n} actuated joints)`, 'success');
    // Clear the badge once the model is up — it only exists to explain an
    // empty viewport while loading or after a failure.
    const statusEl = document.getElementById('urdf-status');
    if (statusEl) statusEl.textContent = '';
}

loadURDF();

async function handleViewportTipSelection(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointerNdc.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointerNdc.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointerNdc, camera);
    const intersects = raycaster.intersectObjects(labwareRoot.children, true);
    const hit = intersects.find(({ object }) => {
        let node = object;
        while (node) {
            if (node.userData?.tipSelection) return true;
            node = node.parent;
        }
        return false;
    });
    if (!hit) return;
    let node = hit.object;
    while (node && !node.userData?.tipSelection) node = node.parent;
    const selection = node?.userData?.tipSelection;
    if (!selection) return;
    const res = await apiCall('/api/tip_selection', 'PUT', selection);
    if (!res?.tip_selection) return;
    state.tipSelection = res.tip_selection;
    const procLocation = document.getElementById('proc-location');
    if (procLocation) procLocation.value = String(selection.location);
    const rowCount = Math.max(1, Number(res.tip_selection.row_count || selection.row_count || 1));
    const columnCount = Math.max(1, Number(res.tip_selection.column_count || selection.column_count || 1));
    const rowEnd = Number(res.tip_selection.row || selection.row || 0) + rowCount;
    const colEnd = Number(res.tip_selection.col || selection.col || 0) + columnCount;
    log(`Selected tip region rows ${Number(res.tip_selection.row || selection.row || 0) + 1}-${rowEnd}, columns ${Number(res.tip_selection.col || selection.col || 0) + 1}-${colEnd}`, 'info');
    syncProcessLabwareSelection();
    void refreshDeckLabwareScene();
}

renderer.domElement.addEventListener('click', (event) => {
    void handleViewportTipSelection(event);
});

// ══════════════════════════════════════════════════════════════════════
// TAB SWITCHING
// ══════════════════════════════════════════════════════════════════════

document.querySelector('.tab-bar').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-tab]');
    if (!btn) return;
    const tabId = btn.dataset.tab;
    document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
    document.getElementById(`tab-${tabId}`)?.classList.add('active');
    if (tabId === 'profile') loadProfile();
});

// ══════════════════════════════════════════════════════════════════════
// API COMMUNICATION
// ══════════════════════════════════════════════════════════════════════

function describeApiError(data, isJson, status) {
    // FastAPI validation failures (422) return `detail` as a LIST of
    // {loc, msg} objects. Interpolating that into a string yields
    // "[object Object]", which hides the actual field that was wrong.
    if (isJson && data) {
        const detail = data.detail;
        if (Array.isArray(detail)) {
            const parts = detail.map(item => {
                const field = Array.isArray(item?.loc)
                    ? item.loc.filter(x => x !== 'body').join('.')
                    : '';
                const msg = item?.msg || 'invalid';
                return field ? `${field}: ${msg}` : msg;
            });
            return parts.length ? `HTTP ${status} — ${parts.join('; ')}` : `HTTP ${status}`;
        }
        const simple = data.error || detail || data.message;
        if (typeof simple === 'string' && simple.trim()) return simple.trim();
        if (simple) return `HTTP ${status} — ${JSON.stringify(simple)}`;
    }
    if (typeof data === 'string' && data.trim()) return data.trim();
    return `HTTP ${status}`;
}

async function apiCall(endpoint, method = 'POST', params = {}) {
    try {
        const url = new URL(`${state.apiBase}${endpoint}`);
        if (method === 'GET') {
            for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
        }
        const opts = { method };
        if (method !== 'GET' && Object.keys(params).length) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(params);
        }
        const res = await fetch(url, opts);
        const contentType = res.headers.get('content-type') || '';
        const isJson = contentType.includes('application/json');
        const data = isJson ? await res.json() : await res.text();
        if (!res.ok) {
            const message = describeApiError(data, isJson, res.status);
            throw new Error(message);
        }
        state.lastApiError = '';
        return data;
    } catch (err) {
        state.lastApiError = err.message;
        log(`API error: ${err.message}`, 'error');
        return null;
    }
}

function connectWebSocket() {
    const wsUrl = `ws://${location.hostname}:8000/ws/state`;
    state.ws = new WebSocket(wsUrl);
    state.ws.onopen = () => { log('WebSocket connected', 'success'); };
    state.ws.onmessage = (event) => {
        try { updateRobotState(JSON.parse(event.data)); } catch (e) { /* ignore */ }
    };
    state.ws.onclose = () => {
        log('WebSocket disconnected', 'info');
        setTimeout(connectWebSocket, 3000);
    };
    state.ws.onerror = () => {};
}

// ══════════════════════════════════════════════════════════════════════
// STATE UPDATE (from WebSocket)
// ══════════════════════════════════════════════════════════════════════

function updateRobotState(data) {
    state.connected = data.connected;
    if (data.machine_id) state.machineId = data.machine_id;
    if (data.active_tip_id) state.activeTipId = data.active_tip_id;
    if (typeof data.active_tip_capacity_ul === 'number') state.activeTipCapacityUl = data.active_tip_capacity_ul;
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    dot.className = data.connected ? 'connected' : '';
    text.textContent = data.connected ? `Connected (${data.controller_type})` : 'Disconnected';
    applyReadiness(data);
    applyGripperUi(data.controller_type);

    // Axis positions — update Jog/Teach tab and drive URDF joints.
    if (data.positions) {
        for (const [axis, value] of Object.entries(data.positions)) {
            state.positions[axis] = value;
            const el = document.getElementById(`pos-${axis.toLowerCase()}`);
            if (el) el.textContent = value.toFixed(3);
        }
        // Mirror G and Zg values on the Gripper tab
        const g2 = document.getElementById('pos-g2');
        const zg2 = document.getElementById('pos-zg2');
        if (g2 && data.positions.G !== undefined) g2.textContent = data.positions.G.toFixed(3);
        if (zg2 && data.positions.Zg !== undefined) zg2.textContent = data.positions.Zg.toFixed(3);
    }

    // Deck state
    if (data.deck) {
        for (let loc = 1; loc <= 9; loc++) {
            const cell = document.querySelector(`.deck-cell[data-loc="${loc}"] .loc-labware`);
            if (cell) {
                const items = data.deck[String(loc)];
                cell.textContent = items && items.length ? items.join(', ') : '-';
            }
        }
        renderDeckAccessoryLabels();
    }
    if (data.deck_details) {
        const signature = JSON.stringify(data.deck_details);
        state.deckDetails = data.deck_details;
            refreshGripperTeachPanel();
        if (signature !== state.deckDetailsSignature) {
            state.deckDetailsSignature = signature;
            void refreshDeckLabwareScene();
        }
        syncProcessLabwareSelection();
    }

    // Motor enable status (I/O tab)
    if (data.motors_enabled) {
        state.motorsEnabled = data.motors_enabled;
        for (const [axis, enabled] of Object.entries(data.motors_enabled)) {
            const dotEl = document.getElementById(`dot-motor-${axis.toLowerCase()}`);
            if (dotEl) dotEl.className = `status-dot ${enabled ? 'on' : 'off'}`;
        }
    }

    if (data.telemetry) {
        state.telemetry = data.telemetry;
        maybeLogPickPlaceTelemetry();
        diagUpdateCurrentGraph();
    }

    const previousTaskStatus = JSON.parse(JSON.stringify(state.taskStatus || {}));
    const previousTaskStatusJson = JSON.stringify(previousTaskStatus || {});
    const previousTipboxInventory = JSON.stringify(state.tipboxInventory || {});
    state.taskStatus = data.task_status || {};
    // Safety net: if the WebSocket says no task is active but
    // commandRunning is still set (e.g. fetch hung or server crashed),
    // re-enable buttons — but only after a 3s grace period so the
    // backend has time to start the task after receiving the HTTP request.
    const taskActive = state.taskStatus.status === 'running' || state.taskStatus.status === 'failed';
    const graceElapsed = Date.now() - state.commandRunningAt > 3000;
    if (state.commandRunning && !taskActive && graceElapsed) {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
    // Sticky-merge motionTargets: accumulate across steps within one
    // task, clear only when the active task itself changes.
    //
    // A single task step's _log_step typically lists ONLY the axes
    // commanded right now (e.g. move_xy_to_pick → {X, Y};
    // move_to_pick_height → {Z, Zg}). With wholesale-replacement,
    // axes that dropped out of the new step's targets fell back to
    // state.positions[axis] in the animate-loop lerp — but
    // state.positions lags physical motion by up to 200 ms because
    // /ws/state broadcasts at 5 Hz. The visible effects were:
    //   * "URDF races to step target then snaps backward to the
    //     lagging telemetry sample then races forward again"
    //   * "After release_plate, the head jumps back to the pickup
    //     position for a frame before finishing the move." That
    //     happens because release_plate_complete is logged with no
    //     targets, which previously cleared motionTargets; the lerp
    //     then chased stale X/Y telemetry back toward the source.
    //
    // Now: merge targets within a task, clear only on task boundary.
    const incomingTargets = state.taskStatus.targets || {};
    const currentTaskName = state.taskStatus.task || null;
    if (currentTaskName !== state._lastTaskName) {
        state.motionTargets = {};
        state._lastTaskName = currentTaskName;
    }
    if (Object.keys(incomingTargets).length > 0) {
        state.motionTargets = { ...state.motionTargets, ...incomingTargets };
    }
    state.tipboxInventory = data.tipbox_inventory || {};
    if (state.taskStatus.task === 'pick_place' && state.taskStatus.step) {
        state.pickPlaceTelemetryActive = true;
    }

    // I/O status indicators
    state.headAttached = data.head_attached ?? false;
    state.goButtonPressed = data.go_button_pressed ?? false;
    state.plateInGripper = data.plate_in_gripper ?? false;
    state.robotDisabled = data.robot_disabled ?? false;

    setDot('dot-head-attached', state.headAttached);
    setDot('dot-go-button', state.goButtonPressed);
    setDot('dot-plate-present', state.plateInGripper);
    setDot('dot-plate-gripper', state.plateInGripper);
    setDot('dot-robot-disable', state.robotDisabled, 'error');

    const headTypeEl = document.getElementById('io-head-type');
    if (headTypeEl) headTypeEl.textContent = data.head_type || '-';
    const previousHeadMode = JSON.stringify(state.headMode);
    const previousTipSelection = JSON.stringify(state.tipSelection);
    const previousTipsOnHead = state.tipsOnHead;
    const previousTipLabware = state.tipLabware;
    const previousAttachedTipLength = state.attachedTipLengthMm;
    const previousTipsOnHeadSelection = JSON.stringify(state.tipsOnHeadSelection);
    state.headType = data.head_type || null;
    state.headMode = data.head_mode || state.headMode;
    state.tipsOnHead = data.tips_on_head ?? false;
    state.tipLabware = data.tip_labware || '';
    state.attachedTipLengthMm = data.attached_tip_length_mm ?? null;
    state.tipsOnHeadMode = data.tips_on_head_mode || null;
    state.tipSelection = data.tip_selection || null;
    state.tipsOnHeadSelection = data.tips_on_head_selection || null;
    if (data.plate_selection) {
        state.processWellSelection = data.plate_selection;
    }
    const tipSelectionChanged =
        previousHeadMode !== JSON.stringify(state.headMode)
        || previousTipSelection !== JSON.stringify(state.tipSelection)
        || previousTipsOnHead !== state.tipsOnHead
        || previousTipLabware !== state.tipLabware
        || previousAttachedTipLength !== state.attachedTipLengthMm
        || previousTipsOnHeadSelection !== JSON.stringify(state.tipsOnHeadSelection)
        || previousTaskStatusJson !== JSON.stringify(state.taskStatus || {})
        || previousTipboxInventory !== JSON.stringify(state.tipboxInventory || {});

    const procHeadType = document.getElementById('proc-head-type');
    if (procHeadType) procHeadType.textContent = data.head_type || '-';
    const procHeadMode = document.getElementById('proc-head-mode');
    if (procHeadMode) procHeadMode.textContent = describeHeadMode(state.headMode);
    if (tipSelectionChanged) {
        state.processWellLegalitySignatures = {};
        renderProcess2DSelector();
        void refreshDeckLabwareScene();
    }
    const contextSignature = JSON.stringify(currentLiquidContext());
    if (contextSignature !== state.liquidContextSignature) {
        void loadLiquidClasses();
    }
    updatePickupRecoveryModal();
    updateTaskPromptModal();
    updateTaskProgressLogging(previousTaskStatus);

    const profHeadType = document.getElementById('prof-head-type');
    if (profHeadType && data.head_type) profHeadType.value = data.head_type;

    // Teachpoints
    if (data.teachpoints) {
        state.teachpoints = data.teachpoints;
        updateDeckMotionMapping();
        const loc = document.getElementById('tp-location')?.value;
        if (loc && data.teachpoints[loc]) {
            const tp = data.teachpoints[loc];
            setVal('tp-x', tp.x?.toFixed(2));
            setVal('tp-y', tp.y?.toFixed(2));
            setVal('tp-z', tp.z?.toFixed(2));
        }
    }
    renderProcess2DSelector();
}

function formatTelemetryValue(value) {
    return typeof value === 'number' ? value.toFixed(3) : String(value);
}

function getAxisTelemetrySummary(axisName) {
    const axis = state.telemetry?.[axisName];
    if (!axis) return null;
    const parts = [];
    for (const key of ['measured_current', 'peak_current', 'last_peak_current_percent', 'last_force_percent', 'current_position_error', 'position_error_max']) {
        if (typeof axis[key] === 'number') parts.push(`${key}=${formatTelemetryValue(axis[key])}`);
    }
    for (const key of ['enabled', 'initialized', 'is_moving']) {
        if (typeof axis[key] === 'boolean') parts.push(`${key}=${axis[key]}`);
    }
    if (axis.last_command?.mode) parts.push(`cmd=${axis.last_command.mode}`);
    if (typeof axis.last_command?.target_position === 'number') parts.push(`target=${formatTelemetryValue(axis.last_command.target_position)}`);
    if (typeof axis.last_command?.position === 'number') parts.push(`pos=${formatTelemetryValue(axis.last_command.position)}`);
    return parts.length ? `${axisName}[${parts.join(' ')}]` : null;
}

function buildPickPlaceTelemetryLine() {
    const parts = [];
    for (const axis of ['Z', 'G', 'Zg']) {
        const summary = getAxisTelemetrySummary(axis);
        if (summary) parts.push(summary);
    }
    return parts.join(' ');
}

function maybeLogPickPlaceTelemetry(force = false) {
    if (!state.pickPlaceTelemetryActive && !force) return;
    const line = buildPickPlaceTelemetryLine();
    if (!line) return;
    const now = Date.now();
    if (!force) {
        if (line === state.lastTelemetrySignature) return;
        if ((now - state.lastTelemetryLogAt) < 1200) return;
    }
    state.lastTelemetrySignature = line;
    state.lastTelemetryLogAt = now;
    log(`Telemetry: ${line}`, 'info');
}

function formatMaybeNumber(value, digits = 3) {
    return typeof value === 'number' ? value.toFixed(digits) : 'n/a';
}

function currentWPositionText() {
    const value = Number(state.positions?.W);
    return Number.isFinite(value) ? `${value.toFixed(3)} uL` : 'unknown uL';
}

function formatAxisPositionText(axisName, digits = 3, units = 'mm') {
    const value = Number(state.positions?.[axisName]);
    return Number.isFinite(value) ? `${value.toFixed(digits)} ${units}` : `unknown ${units}`;
}

function describeTaskPromptAction(endpoint, promptKind) {
    if (promptKind === 'initialize_detect_gripper') {
        if (endpoint === '/api/retry') {
            return {
                acceptedMessage: 'Retry accepted. Rechecking gripper detection before homing G and Zg...',
                pendingDetails: 'Retry accepted.\nRechecking gripper detection now...\nIf detection succeeds, initialization will continue into G and Zg homing.',
            };
        }
        if (endpoint === '/api/ignore') {
            return {
                acceptedMessage: 'Ignore accepted. Continuing as if the gripper is installed and attempting G/Zg homing.',
                pendingDetails: 'Ignore accepted.\nProceeding with gripper initialization despite the failed detect.\nWaiting for G/Zg homing to start...',
            };
        }
        if (endpoint === '/api/abort') {
            return {
                acceptedMessage: 'Abort accepted. Initialization is stopping before gripper homing.',
                pendingDetails: 'Abort accepted.\nStopping initialization before gripper homing.',
            };
        }
    }
    if (promptKind === 'initialize_home_w_axis') {
        if (endpoint === '/api/retry') {
            return {
                acceptedMessage: `Retry accepted. Continuing with W-axis homing from ${currentWPositionText()}...`,
                pendingDetails: `Retry accepted.\nWaiting for live W-axis homing updates...\nCurrent W position: ${currentWPositionText()}`,
            };
        }
        if (endpoint === '/api/ignore') {
            return {
                acceptedMessage: `Ignore accepted. W-axis will remain unhomed at ${currentWPositionText()} while initialization continues.`,
                pendingDetails: `Ignore accepted.\nW-axis homing will be skipped.\nCurrent W position remains ${currentWPositionText()}`,
            };
        }
        if (endpoint === '/api/abort') {
            return {
                acceptedMessage: `Abort accepted. Initialization is stopping with W at ${currentWPositionText()}.`,
                pendingDetails: `Abort accepted.\nStopping initialization now.\nCurrent W position: ${currentWPositionText()}`,
            };
        }
    }
    if (endpoint === '/api/retry') {
        return {
            acceptedMessage: 'Retry accepted. Waiting for the task to continue...',
            pendingDetails: 'Retry accepted.\nWaiting for the task to continue...',
        };
    }
    if (endpoint === '/api/ignore') {
        return {
            acceptedMessage: 'Ignore accepted. Waiting for the task to continue...',
            pendingDetails: 'Ignore accepted.\nWaiting for the task to continue...',
        };
    }
    return {
        acceptedMessage: 'Abort accepted. Waiting for the task to stop...',
        pendingDetails: 'Abort accepted.\nWaiting for the task to stop...',
    };
}

function describeInitializeStepLog(step, status) {
    switch (step) {
        case 'ping_device':
            return 'Initialize: verifying controller communication...';
        case 'query_firmware':
            return 'Initialize: reading controller firmware...';
        case 'detect_gripper':
            return 'Initialize: checking for gripper hardware...';
        case 'detect_head':
            return 'Initialize: checking the installed head...';
        case 'read_home_registers':
            return 'Initialize: reading homed-axis state...';
        case 'check_interlock':
            return 'Initialize: checking safety interlock state...';
        case 'move_z_to_safe_position':
            return 'Initialize: moving Z to a safe position before homing...';
        case 'home_z':
            return `Initialize: homing Z-axis from ${formatAxisPositionText('Z')}...`;
        case 'handle_plate_in_gripper':
            return 'Initialize: checking for a plate in the gripper...';
        case 'home_g':
            return `Initialize: homing gripper G-axis from ${formatAxisPositionText('G')}...`;
        case 'home_zg':
            return `Initialize: homing Zg-axis from ${formatAxisPositionText('Zg')}...`;
        case 'move_zg_to_nesting':
            return 'Initialize: moving Zg to the nesting position...';
        case 'prompt_home_w':
            return status === 'running'
                ? `Initialize: W-axis prompt acknowledged. Current W position is ${currentWPositionText()}.`
                : null;
        case 'home_w':
            return `Initialize: homing W-axis. Current W position is ${currentWPositionText()}.`;
        case 'home_xy':
            return `Initialize: W-axis stage finished at ${currentWPositionText()}. Homing X and Y...`;
        case 'set_light_idle':
            return 'Initialize: setting the robot lights back to idle...';
        case 'finish':
            return 'Initialize: finalizing startup state...';
        default:
            return null;
    }
}

function updateTaskProgressLogging(previousTaskStatus) {
    const current = state.taskStatus || {};
    const previous = previousTaskStatus || {};
    const currentTask = current.task || '';
    const currentStep = current.step || '';
    const currentStatus = current.status || '';
    const previousTask = previous.task || '';
    const previousStep = previous.step || '';
    const previousStatus = previous.status || '';

    if (currentTask === 'initialize' && currentStatus === 'running' && currentStep !== previousStep) {
        const message = describeInitializeStepLog(currentStep, currentStatus);
        if (message) log(message, 'info');
    }

    if (currentTask === 'initialize' && currentStatus === 'running' && currentStep === 'home_w') {
        const wValue = Number(state.positions?.W);
        if (Number.isFinite(wValue)) {
            const lastLogged = state.lastInitializeWHomingLogPosition;
            if (lastLogged === null || Math.abs(wValue - lastLogged) >= 0.5) {
                state.lastInitializeWHomingLogPosition = wValue;
                log(`Initialize: W-axis live position ${wValue.toFixed(3)} uL`, 'info');
            }
        }
    } else {
        state.lastInitializeWHomingLogPosition = null;
    }

    if (
        previousTask === 'initialize'
        && previousStatus === 'running'
        && previousStep === 'home_w'
        && !(currentTask === 'initialize' && currentStatus === 'running' && currentStep === 'home_w')
    ) {
        log(`Initialize: W-axis homing leg ended at ${currentWPositionText()}.`, 'info');
    }

    if (
        previousTask === 'initialize'
        && previousStatus === 'failed'
        && currentTask === 'initialize'
        && currentStatus === 'running'
        && currentStep === 'home_w'
    ) {
        log(`Initialize: retry is active. W-axis homing has started from ${currentWPositionText()}.`, 'info');
    }
}

function updatePickupRecoveryModal() {
    const overlay = document.getElementById('modal-pickup-recovery');
    const messageEl = document.getElementById('pickup-recovery-message');
    const detailsEl = document.getElementById('pickup-recovery-details');
    if (!overlay || !messageEl || !detailsEl) return;

    const prompt = state.taskStatus?.operator_prompt;
    const taskError = state.taskStatus?.error;
    const verification = state.taskStatus?.pickup_verification || {};
    const shouldShow =
        state.taskStatus?.task === 'pick_place'
        && state.taskStatus?.status === 'failed'
        && prompt?.kind === 'pickup_verification_failed';

    if (!shouldShow) {
        overlay.classList.remove('open');
        state.pickupPromptSignature = '';
        return;
    }

    const signature = JSON.stringify({
        step: taskError?.step_name,
        message: taskError?.message,
        attempt: verification.attempt,
    });
    if (state.pickupPromptSignature !== signature) {
        const sensorText =
            verification.sensor_detected === true ? 'plate detected' :
            verification.sensor_detected === false ? 'no plate detected' :
            'sensor unavailable';
        const currentText =
            verification.current_detected ? 'current spike detected' : 'no current change detected';
        const gRuleText =
            verification.g_rule_failed ? 'pickup failed by G position' : 'pickup accepted by G position';
        messageEl.textContent = prompt.message || 'The gripper closed, but the post-grip G position indicates the plate was not picked up.';
        detailsEl.innerHTML = [
            `<strong>Step:</strong> ${taskError?.step_name || 'grip_plate'}`,
            `<strong>Reason:</strong> ${taskError?.message || 'Plate pickup was not detected.'}`,
            `<strong>Attempt:</strong> ${verification.attempt || 1}`,
            `<strong>G after grip:</strong> ${formatMaybeNumber(verification.grip_position_after)} mm`,
            `<strong>G rule:</strong> ${gRuleText}`,
            `<strong>Plate sensor:</strong> ${sensorText}`,
            `<strong>Motor current:</strong> ${currentText} (diagnostic only)`,
            `<strong>Measured current:</strong> ${formatMaybeNumber(verification.measured_current_before)} → ${formatMaybeNumber(verification.measured_current_after)}`,
            `<strong>Current delta:</strong> ${formatMaybeNumber(verification.current_delta)}`,
            `<strong>Peak current:</strong> ${formatMaybeNumber(verification.peak_current_after)}`,
            `<strong>Force %:</strong> ${formatMaybeNumber(verification.force_percent_after, 1)}`,
        ].join('<br>');
        state.pickupPromptSignature = signature;
        log('Plate pickup was not detected. Choose retry, ignore, or abort.', 'error');
    }
    overlay.classList.add('open');
}

function updateTaskPromptModal() {
    const overlay = document.getElementById('modal-task-prompt');
    const titleEl = document.getElementById('task-prompt-title');
    const messageEl = document.getElementById('task-prompt-message');
    const detailsEl = document.getElementById('task-prompt-details');
    const retryBtn = document.getElementById('task-prompt-retry');
    const ignoreBtn = document.getElementById('task-prompt-ignore');
    const abortBtn = document.getElementById('task-prompt-abort');
    if (!overlay || !titleEl || !messageEl || !detailsEl || !retryBtn || !ignoreBtn || !abortBtn) return;

    const prompt = state.taskStatus?.operator_prompt;
    const taskError = state.taskStatus?.error;
    const choices = Array.isArray(prompt?.choices) ? prompt.choices : [];
    const shouldShow =
        state.taskStatus?.status === 'failed'
        && prompt
        && prompt.kind !== 'pickup_verification_failed';

    if (!shouldShow) {
        overlay.classList.remove('open');
        state.taskPromptSignature = '';
        state.taskPromptActionPending = false;
        state.taskPromptPendingDetails = '';
        retryBtn.disabled = false;
        ignoreBtn.disabled = false;
        abortBtn.disabled = false;
        return;
    }

    const signature = JSON.stringify({
        task: state.taskStatus?.task,
        kind: prompt.kind,
        message: prompt.message,
        error: taskError?.message,
    });
    if (state.taskPromptSignature !== signature) {
        titleEl.textContent = prompt.title || 'Operator Action Required';
        messageEl.textContent = prompt.message || 'The active task is waiting for operator input.';
        detailsEl.textContent = taskError?.message || '';
        state.taskPromptSignature = signature;
        state.taskPromptActionPending = false;
        state.taskPromptPendingDetails = '';
        log(prompt.message || 'Operator input is required before the task can continue.', 'error');
    }

    if (state.taskPromptActionPending && state.taskPromptPendingDetails) {
        detailsEl.textContent = state.taskPromptPendingDetails;
    }

    retryBtn.style.display = choices.includes('retry') ? '' : 'none';
    ignoreBtn.style.display = choices.includes('ignore') ? '' : 'none';
    abortBtn.style.display = choices.includes('abort') ? '' : 'none';
    retryBtn.disabled = state.taskPromptActionPending;
    ignoreBtn.disabled = state.taskPromptActionPending;
    abortBtn.disabled = state.taskPromptActionPending;
    overlay.classList.add('open');
}

async function submitTaskPromptAction(endpoint, ignoredMessage, level = 'info') {
    if (state.taskPromptActionPending) return;
    const promptKind = state.taskStatus?.operator_prompt?.kind || '';
    const actionDescription = describeTaskPromptAction(endpoint, promptKind);
    state.taskPromptActionPending = true;
    state.taskPromptPendingDetails = actionDescription.pendingDetails;
    updateTaskPromptModal();
    const res = await apiCall(endpoint, 'POST');
    if (!res) {
        state.taskPromptActionPending = false;
        state.taskPromptPendingDetails = '';
        updateTaskPromptModal();
        return;
    }
    if (res.accepted === false) {
        state.taskPromptActionPending = false;
        state.taskPromptPendingDetails = '';
        updateTaskPromptModal();
        log(ignoredMessage, 'info');
        return;
    }
    log(actionDescription.acceptedMessage, level);
}

function parseProcessCollisionError(message) {
    const text = String(message || '').trim();
    const match = /^(.*?) at location (\d+) is blocked: head footprint overlaps (.*), which meets or exceeds the allowed top plane ([\d.]+) mm for head mode (.*)\.$/i.exec(text);
    if (!match) return null;
    const blockers = [];
    const blockerText = match[3] || '';
    const blockerPattern = /location (\d+) top ([\d.]+) mm/gi;
    let blockerMatch = blockerPattern.exec(blockerText);
    while (blockerMatch) {
        blockers.push({
            location: Number(blockerMatch[1]),
            heightMm: Number(blockerMatch[2]),
        });
        blockerMatch = blockerPattern.exec(blockerText);
    }
    return {
        command: match[1],
        targetLocation: Number(match[2]),
        allowedTopPlaneMm: Number(match[4]),
        headMode: match[5],
        blockers,
        message: text,
    };
}

function getDeckLocationDisplayName(location) {
    const detail = getDeckDetail(location);
    return detail?.name || 'Empty';
}

function renderProcessCollisionModal(collision) {
    const overlay = document.getElementById('modal-process-collision');
    const messageEl = document.getElementById('process-collision-message');
    const commandEl = document.getElementById('process-collision-command');
    const headModeEl = document.getElementById('process-collision-head-mode');
    const topPlaneEl = document.getElementById('process-collision-top-plane');
    const gridEl = document.getElementById('process-collision-grid');
    if (!overlay || !messageEl || !commandEl || !headModeEl || !topPlaneEl || !gridEl) return;

    messageEl.textContent = `${collision.command} at location ${collision.targetLocation} cannot be executed because the shifted head footprint would overlap neighboring occupied deck positions.`;
    commandEl.textContent = `${collision.command} -> location ${collision.targetLocation}`;
    headModeEl.textContent = collision.headMode;
    topPlaneEl.textContent = `${collision.allowedTopPlaneMm.toFixed(1)} mm`;

    const blockerMap = new Map((collision.blockers || []).map(item => [Number(item.location), Number(item.heightMm)]));
    const cards = [];
    for (let location = 1; location <= 9; location++) {
        const isTarget = location === collision.targetLocation;
        const blockerHeight = blockerMap.get(location);
        const detail = getDeckDetail(location);
        const occupied = Boolean(detail);
        const classes = ['collision-slot'];
        if (isTarget) classes.push('target');
        else if (blockerHeight != null) classes.push('blocker');
        else if (occupied) classes.push('occupied');
        const lines = [];
        if (isTarget) {
            lines.push(`Target top ${collision.allowedTopPlaneMm.toFixed(1)} mm`);
            lines.push(getDeckLocationDisplayName(location));
        } else if (blockerHeight != null) {
            lines.push(`Blocking top ${blockerHeight.toFixed(1)} mm`);
            lines.push(getDeckLocationDisplayName(location));
        } else if (occupied) {
            lines.push(getDeckLocationDisplayName(location));
        } else {
            lines.push('No assigned labware');
        }
        cards.push(`
            <div class="${classes.join(' ')}">
                <div class="collision-slot-label">${isTarget ? 'Target' : (blockerHeight != null ? 'Blocking location' : (occupied ? 'Occupied location' : 'Other location'))}</div>
                <div class="collision-slot-title">Location ${location}</div>
                <div class="collision-slot-meta">${lines.join('\n')}</div>
            </div>
        `);
    }
    gridEl.innerHTML = cards.join('');
    overlay.classList.add('open');
}

function closeProcessCollisionModal() {
    document.getElementById('modal-process-collision')?.classList.remove('open');
}

function setDot(id, on, onClass = 'on') {
    const el = document.getElementById(id);
    if (el) el.className = `status-dot ${on ? onClass : 'off'}`;
}

function setVal(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.textContent = val;
}

function populateLabwareSelect(id, definitions, includeEmpty = true) {
    const select = document.getElementById(id);
    if (!select) return;
    select.options.length = 0;
    if (includeEmpty) {
        select.add(new Option('< None >', ''));
    }
    for (const item of definitions) {
        select.add(new Option(item.name, item.id));
    }
}

function populateSimpleSelect(id, items, valueKey = 'name', labelKey = 'name', includeEmpty = true) {
    const select = document.getElementById(id);
    if (!select) return;
    select.options.length = 0;
    if (includeEmpty) {
        select.add(new Option('', ''));
    }
    for (const item of items || []) {
        select.add(new Option(item?.[labelKey] || item?.[valueKey] || '', item?.[valueKey] || ''));
    }
}

async function loadLabwareCatalog() {
    const res = await apiCall('/api/labware', 'GET');
    if (!res?.labware) return;
    state.labwareCatalog = res.labware;
    populateLabwareSelect('cfg-labware', res.labware);
    populateLabwareSelect('proc-labware', res.labware);
    syncProcessLabwareSelection();
    refreshTipboxAssignmentControls();
}

function ensureEditorLinks() {
    const headerActions = document.getElementById('header-actions');
    if (!headerActions) return;
    const ensureLink = (id, label, href) => {
        if (document.getElementById(id)) return;
        const link = document.createElement('a');
        link.id = id;
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = label;
        link.className = 'btn btn-sm';
        headerActions.appendChild(link);
    };
    const ensureButton = (id, label) => {
        if (document.getElementById(id)) return;
        const button = document.createElement('button');
        button.id = id;
        button.type = 'button';
        button.textContent = label;
        button.className = 'btn btn-sm';
        headerActions.appendChild(button);
    };
    ensureLink('btn-open-labware-editor', 'Labware Editor', '/labware-editor');
    ensureLink('btn-open-liquid-class-editor', 'Liquid Class Editor', '/liquid-class-editor');
    ensureLink('btn-open-tip-editor', 'Tip Editor', '/tip-editor');
    ensureButton('btn-start-vision-service', 'Start Vision Service');
    ensureLink('btn-open-vision-calibration', 'Vision Calibration', '/vision-calibration');
    updateVisionUiVisibility();
}

async function loadTipDefinitions(headType = null) {
    const query = headType ? { head_type: headType } : null;
    const res = await apiCall('/api/tips', 'GET', query);
    if (!res?.tips) return;
    state.tipDefinitions = res.tips;
    refreshTipboxAssignmentControls();
}

function isTipBoxDetail(detail) {
    const baseClass = String(detail?.base_class || '').toLowerCase();
    const kind = String(detail?.kind || '').toLowerCase();
    return baseClass === 'tip_box' || kind === 'tip_box';
}

function getTipOptionsForLabware(detail) {
    if (!detail) return [];
    const supported = Array.isArray(detail.supported_tip_ids) ? detail.supported_tip_ids.filter(Boolean) : [];
    if (supported.length) {
        return state.tipDefinitions.filter(item => supported.includes(String(item.tip_id || '')));
    }
    const direct = String(detail.tip_definition_id || '').trim();
    if (direct) {
        return state.tipDefinitions.filter(item => String(item.tip_id || '') === direct);
    }
    const capacity = Number(detail.disposable_tip_capacity_ul || 0);
    if (capacity > 0) {
        return state.tipDefinitions.filter(item => Math.abs(Number(item.capacity_ul || 0) - capacity) < 1e-6);
    }
    return state.tipDefinitions;
}

function ensureTipboxAssignmentControls() {
    const cfgLabware = document.getElementById('cfg-labware');
    if (!cfgLabware) return;
    if (document.getElementById('cfg-tip-id')) return;
    const labwareRow = cfgLabware.closest('.form-row');
    if (!labwareRow?.parentElement) return;
    const wrapper = labwareRow.parentElement;
    const tipRow = document.createElement('div');
    tipRow.className = 'form-row';
    tipRow.id = 'cfg-tip-row';
    tipRow.innerHTML = `<label>Loaded tip</label><select id="cfg-tip-id" style="flex:1"><option value="">Use labware default</option></select>`;
    const fillRow = document.createElement('div');
    fillRow.className = 'form-row';
    fillRow.id = 'cfg-tip-fill-row';
    fillRow.innerHTML = `<label>Tipbox state</label><select id="cfg-tip-fill-state" style="flex:1"><option value="full">Full of tips</option><option value="empty">Empty / discard box</option><option value="preserve">Preserve current</option></select>`;
    wrapper.insertBefore(fillRow, labwareRow.nextSibling);
    wrapper.insertBefore(tipRow, fillRow);
    document.getElementById('cfg-labware')?.addEventListener('change', refreshTipboxAssignmentControls);
}

function refreshTipboxAssignmentControls() {
    ensureTipboxAssignmentControls();
    const tipRow = document.getElementById('cfg-tip-row');
    const fillRow = document.getElementById('cfg-tip-fill-row');
    const tipSelect = document.getElementById('cfg-tip-id');
    const labwareId = document.getElementById('cfg-labware')?.value || '';
    const detail = state.labwareCatalog.find(item => item.id === labwareId);
    const isTipBox = isTipBoxDetail(detail);
    if (tipRow) tipRow.style.display = isTipBox ? 'flex' : 'none';
    if (fillRow) fillRow.style.display = isTipBox ? 'flex' : 'none';
    if (!tipSelect) return;
    tipSelect.innerHTML = `<option value="">Use labware default</option>`;
    if (!isTipBox) return;
    const options = getTipOptionsForLabware(detail);
    for (const tip of options) {
        tipSelect.add(new Option(tip.label || tip.tip_id || '', tip.tip_id || ''));
    }
    const preferred = String(detail?.tip_definition_id || options[0]?.tip_id || '');
    if (preferred) tipSelect.value = preferred;
}

function currentLiquidContext() {
    const headType = state.headType || document.getElementById('prof-head-type')?.value || 'HT_96_D_70';
    const selectedTipRef = document.getElementById('tp-tip-capacity')?.value || '';
    const selectedTip = getTipDefinitionForSelection(headType, selectedTipRef);
    const tipCapacityUl = Number(
        state.activeTipCapacityUl
        || selectedTip?.capacity_ul
        || 0,
    );
    const tipId = state.activeTipId || selectedTip?.tip_id || '';
    const machineId = state.machineId || document.getElementById('conn-machine-id')?.value || 'SIM_BRAVO';
    return { machine_id: machineId, head_type: headType, tip_id: tipId, tip_capacity_ul: tipCapacityUl };
}

async function loadLiquidClasses() {
    const contextRes = await apiCall('/api/liquid_context', 'GET');
    const context = contextRes ? {
        machine_id: contextRes.machine_id || currentLiquidContext().machine_id,
        head_type: contextRes.head_type || currentLiquidContext().head_type,
        tip_id: contextRes.tip_id || currentLiquidContext().tip_id,
        tip_capacity_ul: Number(contextRes.tip_capacity_ul ?? currentLiquidContext().tip_capacity_ul ?? 0),
    } : currentLiquidContext();
    state.machineId = context.machine_id || state.machineId;
    state.headType = context.head_type || state.headType;
    state.activeTipId = context.tip_id || state.activeTipId;
    state.activeTipCapacityUl = Number(context.tip_capacity_ul || state.activeTipCapacityUl || 0);
    state.liquidContextSignature = JSON.stringify(context);
    // Scope by device + head only; the server narrows by tip based on whether
    // tips are physically on the head (tips off -> all classes for this
    // machine/head; tips on -> just the loaded tip). It never returns classes
    // for other devices/heads, so we display its result directly.
    const res = await apiCall('/api/liquid_classes', 'GET', {
        machine_id: context.machine_id,
        head_type: context.head_type,
    });
    if (!res?.liquid_classes) return;
    const compatible = res.liquid_classes;
    state.liquidClasses = compatible;
    const selectIds = ['asp-liquid-class', 'disp-liquid-class', 'mix-liquid-class'];
    for (const selectId of selectIds) {
        const select = document.getElementById(selectId);
        if (!select) continue;
        select.options.length = 0;
        select.add(new Option('', ''));
        for (const item of compatible) {
            const label = item.tip_id
                ? `${item.name} (${item.tip_id})`
                : item.tip_capacity_ul
                ? `${item.name} (${Number(item.tip_capacity_ul).toFixed(0)} uL)`
                : item.name;
            select.add(new Option(label, item.name || ''));
        }
    }
}

async function loadPipetteTechniques() {
    const res = await apiCall('/api/pipette_techniques', 'GET');
    if (!res?.pipette_techniques) return;
    state.pipetteTechniques = res.pipette_techniques;
    ['asp-technique', 'disp-technique', 'mix-technique'].forEach((id) => {
        populateSimpleSelect(id, state.pipetteTechniques, 'name', 'name', true);
    });
}

async function refreshStateNow() {
    const res = await apiCall('/api/state', 'GET');
    if (res) updateRobotState(res);
}

// ══════════════════════════════════════════════════════════════════════
// HEADER BUTTONS
// ══════════════════════════════════════════════════════════════════════

document.getElementById('btn-connect').addEventListener('click', async () => {
    log('Connecting...', 'info');
    if (!state.profileLoaded) {
        await loadProfile();
    }
    // Read current form values so user doesn't have to save profile first
    const ctrlType = state.profileLoaded ? (document.getElementById('conn-controller-type')?.value || undefined) : undefined;
    const addr = state.profileLoaded ? (document.getElementById('conn-address')?.value || undefined) : undefined;
    const serialPort = state.profileLoaded ? (document.getElementById('conn-serial-port')?.value || undefined) : undefined;
    const body = {};
    if (ctrlType) body.controller_type = ctrlType;
    if (addr) body.address = addr;
    if (serialPort) body.serial_port = serialPort;
    const res = await apiCall('/api/connect', 'POST', body);
    if (res) {
        // Connect opens the transport only — it does not run the
        // initialization sequence. Saying otherwise led operators to
        // command motion against an uninitialized, unhomed machine.
        log(`Connected (${res.controller}) — press Initialize before commanding motion`, 'success');
        updateConnectButton(true);
        await refreshStateNow();
    }
});

document.getElementById('btn-theme-toggle')?.addEventListener('click', () => {
    applyTheme(state.theme === 'light' ? 'dark' : 'light');
});

// Processes-tab commands that work without a gripper (Bravo SRT). Everything
// else (plate stacking, mounting, delidding, barcode, stack-height scan)
// needs the gripper.
const GRIPPERLESS_PROCESS_COMMANDS = new Set([
    'Aspirate', 'Dispense', 'Mix', 'Tips On', 'Tips Off',
]);

// Hide gripper-only UI for gripperless devices — the Bravo SRT has no G/Zg
// axes. Hides the Gripper tab and the gripper-only Processes-tab commands.
// Falls back to the controller-type dropdown when no explicit type is given
// (e.g. while disconnected).
function applyGripperUi(controllerType) {
    const ct = controllerType || document.getElementById('conn-controller-type')?.value;
    const gripperless = ct === 'agile_srt';

    const tabBtn = document.querySelector('.tab-bar button[data-tab="gripper"]');
    if (tabBtn) {
        tabBtn.style.display = gripperless ? 'none' : '';
        if (gripperless && tabBtn.classList.contains('active')) {
            document.querySelector('.tab-bar button[data-tab="jog"]')?.click();
        }
    }

    const cmdSel = document.getElementById('proc-command');
    if (cmdSel) {
        for (const opt of cmdSel.options) {
            const allowed = !gripperless || GRIPPERLESS_PROCESS_COMMANDS.has(opt.value);
            opt.hidden = !allowed;
            opt.disabled = !allowed;
        }
        const cur = cmdSel.options[cmdSel.selectedIndex];
        if (cur && cur.hidden) {
            cmdSel.value = 'Aspirate';
            cmdSel.dispatchEvent(new Event('change'));
        }
    }
}

function updateConnectionFields(controllerType) {
    applyGripperUi(controllerType);
    const ethFields = document.getElementById('conn-ethernet-fields');
    const serFields = document.getElementById('conn-serial-fields');
    if (!ethFields || !serFields) return;
    const isSerial = controllerType === 'darwin_serial';
    const isSim = controllerType === 'simulation';
    ethFields.style.display = (isSim || isSerial) ? 'none' : 'block';
    serFields.style.display = isSerial ? 'block' : 'none';
}

// Show/hide connection fields when controller type changes
document.getElementById('conn-controller-type')?.addEventListener('change', (e) => {
    updateConnectionFields(e.target.value);
});

// ── Readiness ───────────────────────────────────────────────────────────────
// The robot has three gates before it can be driven: connected, initialized,
// homed. Those were previously invisible — the operator found out by pressing
// something and watching it fail. The status buttons now report the state they
// control, and anything that moves the machine explains what is missing.
//
// Controls are marked with a class rather than the `disabled` attribute:
// disabled elements do not fire hover in Chrome, so their tooltip never shows.
// A capture-phase listener below swallows the click instead.

const READINESS_GATES = [
    { key: 'connected', why: 'Not connected — press Connect first.' },
    { key: 'initialized', why: 'Not initialized — press Initialize first.' },
    { key: 'homed', why: 'Not homed — press Home All first.' },
];

function readinessBlocker() {
    for (const gate of READINESS_GATES) {
        if (!state.readiness?.[gate.key]) return gate.why;
    }
    return null;
}

function setStatusButton(id, ready, readyText, idleText) {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.textContent = ready ? readyText : idleText;
    btn.classList.toggle('btn-ready', ready);
    // Grey, not red — red means destructive (Abort, Quit), and a gate that has
    // simply not been satisfied yet is not an error.
    btn.classList.toggle('btn-idle', !ready);
}

function applyReadiness(snapshot) {
    state.readiness = {
        connected: Boolean(snapshot?.connected),
        initialized: Boolean(snapshot?.initialized),
        homed: Boolean(snapshot?.homed),
    };

    setStatusButton('btn-connect', state.readiness.connected, 'Connected', 'Connect');
    setStatusButton('btn-init', state.readiness.initialized, 'Initialized', 'Initialize');
    setStatusButton('btn-home', state.readiness.homed, 'Homed', 'Home All');

    // Connect and Initialize are themselves the remedy for an earlier gate, so
    // they are gated only by what must come before them.
    const initBlocker = state.readiness.connected ? null : READINESS_GATES[0].why;
    markReadiness(document.getElementById('btn-init'), initBlocker);
    markReadiness(document.getElementById('btn-home'),
        state.readiness.initialized ? null : (initBlocker || READINESS_GATES[1].why));

    const blocker = readinessBlocker();
    for (const id of MOTION_BUTTON_IDS) {
        // Initialize and Home All are the remedy for the gate they would
        // otherwise fail, and were gated individually just above. Applying the
        // blanket blocker to them too would disable Initialize and tell you,
        // via its own tooltip, to press Initialize.
        if (SELF_REMEDY_BUTTON_IDS.has(id)) continue;
        markReadiness(document.getElementById(id), blocker);
    }
    document.querySelectorAll('.jog-btn[data-jog]').forEach(el => markReadiness(el, blocker));
}

function markReadiness(el, blocker) {
    if (!el) return;
    if (blocker) {
        el.classList.add('not-ready');
        el.dataset.notReady = blocker;
        el.title = blocker;
    } else {
        el.classList.remove('not-ready');
        delete el.dataset.notReady;
        if (el.title && el.title.startsWith('Not ')) el.removeAttribute('title');
    }
}

// Capture phase: runs before the control's own handler, so the action never
// starts. Says why, rather than failing silently.
document.addEventListener('click', (event) => {
    const el = event.target?.closest?.('.not-ready');
    if (!el) return;
    event.preventDefault();
    event.stopPropagation();
    log(el.dataset.notReady || 'Not available yet', 'error');
}, true);

function updateConnectButton(connected) {
    setStatusButton('btn-connect', connected, 'Connected', 'Connect');
}

document.getElementById('btn-init').addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Initializing...', 'info');
        const res = await apiCall('/api/initialize');
        if (res) log('Initialized', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-home').addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Homing all axes...', 'info');
        const res = await apiCall('/api/home');
        if (res) log(`Home: ${res.status}`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-abort').addEventListener('click', async () => {
    const res = await apiCall('/api/abort');
    if (res) log('Aborted', 'error');
});

document.getElementById('btn-quit')?.addEventListener('click', async () => {
    if (!confirm('Kill the backend server?\n\nThis hard-exits the Python process. Any in-flight task will be aborted.')) return;
    try {
        await fetch('/api/shutdown', { method: 'POST' });
    } catch (_) { /* expected — server is dying */ }
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font:14px/1.5 system-ui;color:#888;background:#111">Server shut down. You can close this tab.</div>';
});

document.getElementById('pickup-recovery-retry')?.addEventListener('click', async () => {
    const res = await apiCall('/api/retry');
    if (res) log('Retrying plate pickup...', 'info');
});

document.getElementById('pickup-recovery-ignore')?.addEventListener('click', async () => {
    const res = await apiCall('/api/ignore');
    if (res) log('Ignoring missing plate and continuing pick/place...', 'info');
});

document.getElementById('pickup-recovery-abort')?.addEventListener('click', async () => {
    const res = await apiCall('/api/abort');
    if (res) log('Pick/place aborted after missing plate pickup.', 'error');
});

document.getElementById('task-prompt-retry')?.addEventListener('click', async () => {
    await submitTaskPromptAction(
        '/api/retry',
        'That operator prompt is no longer waiting for input.',
        'info',
    );
});

document.getElementById('task-prompt-ignore')?.addEventListener('click', async () => {
    await submitTaskPromptAction(
        '/api/ignore',
        'That operator prompt is no longer waiting for input.',
        'info',
    );
});

document.getElementById('task-prompt-abort')?.addEventListener('click', async () => {
    await submitTaskPromptAction(
        '/api/abort',
        'That operator prompt is no longer waiting for input.',
        'error',
    );
});

// ══════════════════════════════════════════════════════════════════════
// JOG / TEACH TAB
// ══════════════════════════════════════════════════════════════════════

document.getElementById('process-collision-ok')?.addEventListener('click', closeProcessCollisionModal);
document.getElementById('process-collision-close')?.addEventListener('click', closeProcessCollisionModal);
document.getElementById('modal-process-collision')?.addEventListener('click', (event) => {
    if (event.target?.id === 'modal-process-collision') closeProcessCollisionModal();
});

function getJogStep(axis) {
    if (axis === 'x' || axis === 'y') return parseFloat(document.getElementById('jog-step-xy')?.value || '5');
    if (axis === 'z') return parseFloat(document.getElementById('jog-step-z')?.value || '5');
    if (axis === 'w') return parseFloat(document.getElementById('jog-step-w')?.value || '5');
    if (axis === 'zg') return parseFloat(document.getElementById('jog-step-zg')?.value || '5');
    if (axis === 'g') return parseFloat(document.getElementById('jog-step-g')?.value || '5');
    return 5.0;
}

function getSelectedSpeed() {
    const value = (document.getElementById('jog-speed')?.value || 'Medium').toUpperCase();
    if (value === 'FAST') return 'FAST';
    if (value === 'SLOW') return 'SLOW';
    return 'MED';
}

document.querySelectorAll('.jog-btn[data-jog]').forEach(btn => {
    btn.addEventListener('click', async () => {
        if (state.commandRunning) return;
        state.commandRunning = true;
        setMotionButtonsEnabled(false);
        try {
            const axis = btn.dataset.jog;
            const dir = parseInt(btn.dataset.dir);
            const step = getJogStep(axis);
            const speed = getSelectedSpeed();
            const body = { axis, step, direction: dir, speed };
            const peakCurrent = (axis === 'z') ? diagGetPeakCurrent() : undefined;
            if (peakCurrent !== undefined) {
                body.peak_current = peakCurrent;
                log(`Force-limited jog Z ${dir > 0 ? '+' : '-'}${step} (${speed}, limit ${peakCurrent}A)`, 'info');
            } else {
                log(`Jog ${axis.toUpperCase()} ${dir > 0 ? '+' : '-'}${step} (${speed})`, 'info');
            }
            const res = await apiCall('/api/jog', 'POST', body);
            if (res?.position !== undefined) {
                log(`${axis.toUpperCase()} → ${res.position.toFixed(3)}`, 'success');
            } else if (res?.error) {
                log(`Jog error: ${res.error}`, 'error');
            }
        } finally {
            state.commandRunning = false;
            setMotionButtonsEnabled(true);
        }
    });
});

// Per-axis home buttons
for (const axis of ['x', 'y', 'z', 'w', 'g', 'zg']) {
    const btn = document.getElementById(`btn-home-${axis}`);
    if (btn) {
        btn.addEventListener('click', async () => {
            if (state.commandRunning) return;
            state.commandRunning = true;
            setMotionButtonsEnabled(false);
            try {
                log(`Homing ${axis.toUpperCase()}...`, 'info');
                const res = await apiCall('/api/home_axis', 'POST', { axis });
                if (res) log(`${axis.toUpperCase()} homed`, 'success');
            } finally {
                state.commandRunning = false;
                setMotionButtonsEnabled(true);
            }
        });
    }
}

// Multiple axes
document.getElementById('btn-home-xyz')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Homing XYZ...', 'info');
        // One request, not a loop. Sequencing axes here put X and Y before Z,
        // which drags the head sideways before it has lifted. The server orders
        // them for clearance — do not re-introduce a client-side sequence.
        const res = await apiCall('/api/home_axis', 'POST', { axes: ['X', 'Y', 'Z'] });
        if (res) log(`Homed ${(res.axes || ['X', 'Y', 'Z']).join(' → ')}`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-enable-all')?.addEventListener('click', async () => {
    const res = await apiCall('/api/motor/enable_all');
    if (res) log('All motors enabled', 'success');
});

document.getElementById('btn-disable-all')?.addEventListener('click', async () => {
    const res = await apiCall('/api/motor/disable_all');
    if (res) log('All motors disabled', 'info');
});

// Teachpoint location change — load values
document.getElementById('tp-location')?.addEventListener('change', async (e) => {
    const loc = e.target.value;
    const res = await apiCall(`/api/teachpoint/${loc}`, 'GET');
    if (res?.teachpoint) {
        setVal('tp-x', res.teachpoint.x?.toFixed(2));
        setVal('tp-y', res.teachpoint.y?.toFixed(2));
        setVal('tp-z', res.teachpoint.z?.toFixed(2));
    }
});

document.getElementById('btn-tp-move')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        const loc = parseInt(document.getElementById('tp-location').value);
        const speed = getSelectedSpeed();
        log(`Moving to location ${loc} (${speed})...`, 'info');
        const res = await apiCall('/api/move_to_location', 'POST', {
            location: loc,
            approach_height: 0,
            only_move_z: false,
            speed,
        });
        if (res) log(`Moved to location ${loc}`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-tp-approach')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        const loc = parseInt(document.getElementById('tp-location').value);
        const approachHeight = parseFloat(document.getElementById('tp-approach-height')?.value || '20');
        const speed = getSelectedSpeed();
        log(`Approaching location ${loc} (${speed})...`, 'info');
        const res = await apiCall('/api/move_to_location', 'POST', {
            location: loc,
            approach_height: approachHeight,
            only_move_z: false,
            speed,
        });
        if (res) log(`Approached location ${loc}`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-tp-teach')?.addEventListener('click', async () => {
    const loc = parseInt(document.getElementById('tp-location').value);
    const headType = document.getElementById('prof-head-type')?.value || '';
    const tipRef = document.getElementById('tp-tip-capacity')?.value || '';
    const selectedTip = getTipDefinitionForSelection(headType, tipRef);
    const tipCapacity = Number(selectedTip?.capacity_ul || 0);
    const tipHeight = getTipHeightForCapacity(headType, tipRef);
    const x = document.getElementById('pos-x')?.textContent || '0.000';
    const y = document.getElementById('pos-y')?.textContent || '0.000';
    const z = document.getElementById('pos-z')?.textContent || '0.000';
    const message = [
        `Teach location ${loc}?`,
        ``,
        `X: ${x} mm`,
        `Y: ${y} mm`,
        `Z: ${z} mm`,
        `Teach tip: ${selectedTip?.label || `${tipCapacity.toFixed(0)} uL`}`,
        `Tip height: ${tipHeight == null ? 'unknown' : `${tipHeight.toFixed(1)} mm`}`,
        '',
        `Saving into profile: ${state.activeProfileName || '(unknown)'}`,
    ].join('\n');
    if (!window.confirm(message)) return;
    log(`Teaching location ${loc} using ${selectedTip?.label || `${tipCapacity.toFixed(0)} uL`} tip...`, 'info');
    const res = await apiCall(`/api/teachpoint/${loc}/teach_current`, 'POST', { tip_id: selectedTip?.tip_id || '', tip_capacity: tipCapacity });
    if (res?.teachpoint) {
        setVal('tp-x', res.teachpoint.x?.toFixed(2));
        setVal('tp-y', res.teachpoint.y?.toFixed(2));
        setVal('tp-z', res.teachpoint.z?.toFixed(2));
        state.activeTipId = res.teach_tip_id || selectedTip?.tip_id || state.activeTipId;
        state.activeTipCapacityUl = Number(res.teach_tip_capacity || tipCapacity || 0);
        const intoProfile = res.profile ? ` into profile '${res.profile}'` : '';
        log(`Location ${loc} taught${intoProfile} with ${selectedTip?.label || `${res.teach_tip_capacity?.toFixed?.(0) || tipCapacity.toFixed(0)} uL`} tip`, 'success');
    }
});

document.getElementById('tp-tip-capacity')?.addEventListener('change', (e) => {
    const label = document.getElementById('tp-tip-height-label');
    const headType = document.getElementById('prof-head-type')?.value || '';
    const selected = getTipDefinitionForSelection(headType, e.target.value);
    if (label) {
        const height = getTipHeightForCapacity(headType, e.target.value);
        label.textContent = height == null ? 'unknown' : `${height.toFixed(1)} mm`;
    }
    state.activeTipId = selected?.tip_id || String(e.target.value || '');
    state.activeTipCapacityUl = Number(selected?.capacity_ul || 0);
    void loadLiquidClasses();
});

document.getElementById('prof-head-type')?.addEventListener('change', (e) => {
    state.headType = e.target.value || state.headType;
    void loadLiquidClasses();
});

document.getElementById('conn-machine-id')?.addEventListener('change', (e) => {
    state.machineId = e.target.value || state.machineId;
    void loadLiquidClasses();
});

document.getElementById('btn-tp-safe-z')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        const speed = getSelectedSpeed();
        log(`Moving to safe Z (${speed})...`, 'info');
        const res = await apiCall('/api/move_safe_z', 'POST', { speed });
        if (res) log('At safe Z', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

// ══════════════════════════════════════════════════════════════════════
// GRIPPER TAB
// ══════════════════════════════════════════════════════════════════════

// ── Gripper teaching ────────────────────────────────────────────────────────
// The Y offset is a property of how the gripper is mounted relative to the
// head, so there is one value per machine. The Location tells us which
// teachpoint to measure against; the labware comes from the deck so the two
// can never disagree.

function gripTeachLocation() {
    return parseInt(document.getElementById('grip-location')?.value || '1', 10);
}

function refreshGripperTeachPanel() {
    const yField = document.getElementById('grip-y-offset');
    if (yField) {
        const current = state.profileGripperYOffset;
        // A right-aligned mono readout, like the teachpoint values: the offset
        // is read-only here and a number input clipped it behind its spinner.
        yField.textContent = (typeof current === 'number') ? current.toFixed(2) : '—';
    }

    const note = document.getElementById('grip-labware-note');
    if (!note) return;
    const labware = getDeckDetail(gripTeachLocation());
    const name = labware?.name || labware || null;
    if (name) {
        note.textContent = `teaching against: ${name}`;
        note.style.color = '';
    } else {
        note.textContent = 'no labware at this location — nothing to teach against';
        note.style.color = 'var(--danger, #c66)';
    }
}

document.getElementById('grip-location')?.addEventListener('change', refreshGripperTeachPanel);

async function gripperTeachMove(approachHeight, label) {
    if (state.commandRunning) return;
    const loc = gripTeachLocation();
    if (!getDeckDetail(loc)) {
        log(`Location ${loc} has no labware assigned — assign one before teaching`, 'error');
        return;
    }
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log(`${label} gripper at location ${loc}...`, 'info');
        const res = await apiCall('/api/gripper/move_to_location', 'POST', {
            location: loc,
            approach_height: approachHeight,
            speed: getSelectedSpeed(),
        });
        if (res) log(`Gripper ${res.status} at location ${loc}`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
}

document.getElementById('btn-grip-move')?.addEventListener('click', () => gripperTeachMove(0, 'Moving'));

document.getElementById('btn-grip-approach')?.addEventListener('click', () => {
    const clearance = parseFloat(document.getElementById('grip-approach')?.value || '20');
    gripperTeachMove(clearance, 'Approaching');
});

document.getElementById('btn-grip-teach')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    const loc = gripTeachLocation();
    const labware = getDeckDetail(loc);
    if (!labware) {
        log(`Location ${loc} has no labware assigned — nothing to teach against`, 'error');
        return;
    }
    const name = labware?.name || labware;
    if (!window.confirm(
        `Teach the gripper Y offset from the current position?\n\n`
        + `Location: ${loc}\n`
        + `Labware: ${name}\n`
        + `Profile: ${state.activeProfileName || '(active)'}\n\n`
        + 'Jog the gripper until it is centred on the plate first. '
        + 'This overwrites the machine-wide gripper Y offset and saves immediately.'
    )) return;

    const res = await apiCall('/api/gripper/teach_y_offset', 'POST', { location: loc });
    if (res) {
        state.profileGripperYOffset = res.y_offset;
        refreshGripperTeachPanel();
        log(
            `Gripper Y offset taught at location ${loc}: ${res.y_offset.toFixed(3)} mm `
            + `(was ${res.previous_y_offset.toFixed(3)}) — saved to profile '${res.profile}'`,
            'success',
        );
    }
});

document.getElementById('btn-open-gripper')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Opening gripper...', 'info');
        const res = await apiCall('/api/gripper/open');
        if (res) log('Gripper opened', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-close-gripper')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Closing gripper...', 'info');
        const res = await apiCall('/api/gripper/close');
        if (res) log('Gripper closed', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-dock-gripper')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Docking gripper...', 'info');
        const res = await apiCall('/api/gripper/dock');
        if (res) log('Gripper docked', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-pick-ab')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        const locA = parseInt(document.getElementById('pp-loc-a').value);
        const locB = parseInt(document.getElementById('pp-loc-b').value);
        state.pickPlaceTelemetryActive = true;
        state.lastTelemetryLogAt = 0;
        state.lastTelemetrySignature = '';
        log(`Pick ${locA} → Place ${locB}...`, 'info');
        const res = await apiCall('/api/pick_place', 'POST', { from_location: locA, to_location: locB });
        if (res?.diagnostics) {
            const d = res.diagnostics;
            log(
                `Pick/Place plan: teachZ=${d.source_teach_z?.toFixed?.(3)} pickH=${d.source_pick_height_mm?.toFixed?.(3)} supportH=${d.source_support_height_mm?.toFixed?.(3)} topZ=${d.source_top_z?.toFixed?.(3)} gripZ=${d.source_grip_plane_z?.toFixed?.(3)} pick(Z=${d.pick_z?.toFixed?.(3)}, Zg=${d.pick_zg?.toFixed?.(3)}) carry(Z=${d.carry_z?.toFixed?.(3)}, Zg=${d.carry_zg?.toFixed?.(3)}) place(Z=${d.place_z?.toFixed?.(3)}, Zg=${d.place_zg?.toFixed?.(3)}) Goffset=${d.gripper_offset_mm?.toFixed?.(3)}`,
                'info',
            );
        }
        maybeLogPickPlaceTelemetry(true);
        state.pickPlaceTelemetryActive = false;
        if (res) log(`Pick & Place complete`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

document.getElementById('btn-pick-ba')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        const locA = parseInt(document.getElementById('pp-loc-a').value);
        const locB = parseInt(document.getElementById('pp-loc-b').value);
        state.pickPlaceTelemetryActive = true;
        state.lastTelemetryLogAt = 0;
        state.lastTelemetrySignature = '';
        log(`Pick ${locB} → Place ${locA}...`, 'info');
        const res = await apiCall('/api/pick_place', 'POST', { from_location: locB, to_location: locA });
        if (res?.diagnostics) {
            const d = res.diagnostics;
            log(
                `Pick/Place plan: teachZ=${d.source_teach_z?.toFixed?.(3)} pickH=${d.source_pick_height_mm?.toFixed?.(3)} supportH=${d.source_support_height_mm?.toFixed?.(3)} topZ=${d.source_top_z?.toFixed?.(3)} gripZ=${d.source_grip_plane_z?.toFixed?.(3)} pick(Z=${d.pick_z?.toFixed?.(3)}, Zg=${d.pick_zg?.toFixed?.(3)}) carry(Z=${d.carry_z?.toFixed?.(3)}, Zg=${d.carry_zg?.toFixed?.(3)}) place(Z=${d.place_z?.toFixed?.(3)}, Zg=${d.place_zg?.toFixed?.(3)}) Goffset=${d.gripper_offset_mm?.toFixed?.(3)}`,
                'info',
            );
        }
        maybeLogPickPlaceTelemetry(true);
        state.pickPlaceTelemetryActive = false;
        if (res) log(`Pick & Place complete`, 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

// ══════════════════════════════════════════════════════════════════════
// I/O TAB
// ══════════════════════════════════════════════════════════════════════

document.getElementById('btn-refresh-head')?.addEventListener('click', async () => {
    log('Refreshing I/O status...', 'info');
    const res = await apiCall('/api/state', 'GET');
    if (res) {
        updateRobotState(res);
        log(`I/O refreshed: head ${res.head_attached ? 'attached' : 'not attached'}`, 'success');
    }
});

document.querySelectorAll('.deck-cell').forEach(cell => {
    cell.addEventListener('click', () => {
        const loc = cell.dataset.loc;
        const cfgLocation = document.getElementById('cfg-location');
        const procLocation = document.getElementById('proc-location');
        if (cfgLocation) cfgLocation.value = loc;
        if (procLocation) procLocation.value = loc;
        syncProcessLabwareSelection();
        document.querySelectorAll('.deck-cell').forEach(el => el.classList.remove('selected'));
        cell.classList.add('selected');
    });
});

document.getElementById('btn-cfg-assign-labware')?.addEventListener('click', async () => {
    const location = parseInt(document.getElementById('cfg-location')?.value || '0');
    const labwareId = document.getElementById('cfg-labware')?.value || '';
    const isLidded = document.getElementById('cfg-lidded')?.checked ?? false;
    const isSealed = document.getElementById('cfg-sealed')?.checked ?? false;
    const tipDefinitionId = document.getElementById('cfg-tip-id')?.value || '';
    const tipboxFillState = document.getElementById('cfg-tip-fill-state')?.value || 'full';
    if (!location || !labwareId) {
        log('Select a location and labware first', 'error');
        return;
    }
    const selected = state.labwareCatalog.find(item => item.id === labwareId);
    log(`Assigning ${selected?.name || labwareId} to location ${location}...`, 'info');
    const res = await apiCall(`/api/deck/${location}/labware`, 'PUT', {
        labware_id: labwareId,
        is_lidded: isLidded,
        is_sealed: isSealed,
        tip_definition_id: tipDefinitionId,
        tipbox_fill_state: tipboxFillState,
    });
    if (res) {
        await refreshStateNow();
        log(`Assigned ${res.labware?.name || 'labware'} to location ${location}`, 'success');
    }
});

document.getElementById('btn-cfg-clear-labware')?.addEventListener('click', async () => {
    const location = parseInt(document.getElementById('cfg-location')?.value || '0');
    if (!location) {
        log('Select a location first', 'error');
        return;
    }
    log(`Clearing location ${location}...`, 'info');
    const res = await apiCall(`/api/deck/${location}/labware`, 'DELETE');
    if (res) {
        await refreshStateNow();
        log(`Cleared location ${location}`, 'success');
    }
});

document.getElementById('btn-vision-verify')?.addEventListener('click', async () => {
    if (!state.visionEnabled) {
        log('Vision feature is disabled in the active profile', 'error');
        return;
    }
    log('Running deck verification...', 'info');
    const res = await apiCall('/api/vision/verify', 'POST');
    const report = res?.report;
    if (!report?.summary) return;
    const summary = report.summary;
    log(
        `Deck verification ${summary.status}: ${summary.slot_count} slot(s), ${summary.unknown_slots} unknown, ${summary.empty_ok_slots} empty OK`,
        summary.pass ? 'success' : 'info',
    );
    for (const slot of report.slots || []) {
        if (slot.status === 'empty_ok') continue;
        const expected = slot.expected_labware?.name || 'unknown';
        log(`Vision slot ${slot.location}: ${slot.status} for ${expected} (${slot.reason})`, 'info');
    }
});

document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.id !== 'btn-start-vision-service') return;
    if (!state.visionEnabled) {
        log('Vision feature is disabled in the active profile', 'error');
        return;
    }
    log('Starting vision service...', 'info');
    const res = await apiCall('/api/vision/service/start', 'POST');
    if (!res) return;
    if (res.status === 'already_running') {
        log('Vision service is already running', 'success');
        return;
    }
    if (res.status === 'starting') {
        log('Vision service launch requested', 'success');
    }
});

// ══════════════════════════════════════════════════════════════════════
// PROCESSES TAB
// ══════════════════════════════════════════════════════════════════════

document.getElementById('btn-exec-command')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
    const command = document.getElementById('proc-command').value;
    const location = parseInt(document.getElementById('proc-location').value);

    const params = { command, location };

    if (command === 'Aspirate') {
        params.volume = parseFloat(document.getElementById('asp-volume')?.value || '0');
        params.pre_aspirate = parseFloat(document.getElementById('asp-pre')?.value || '0');
        params.post_aspirate = parseFloat(document.getElementById('asp-post')?.value || '0');
        params.distance_from_bottom = parseFloat(document.getElementById('asp-dist-bottom')?.value || '2');
        params.liquid_class = document.getElementById('asp-liquid-class')?.value || '';
        params.pipette_technique = document.getElementById('asp-technique')?.value || '';
        params.dynamic_tip_extension = parseFloat(document.getElementById('asp-dyn-ext')?.value || '0');
        params.tip_touch = Boolean(document.getElementById('asp-tip-touch')?.checked);
    } else if (command === 'Dispense') {
        params.empty_tips = Boolean(document.getElementById('disp-empty-tips')?.checked);
        params.volume = parseFloat(document.getElementById('disp-volume')?.value || '0');
        params.blowout = parseFloat(document.getElementById('disp-blowout')?.value || '0');
        params.distance_from_bottom = parseFloat(document.getElementById('disp-dist-bottom')?.value || '2');
        params.liquid_class = document.getElementById('disp-liquid-class')?.value || '';
        params.pipette_technique = document.getElementById('disp-technique')?.value || '';
        params.dynamic_tip_retraction = parseFloat(document.getElementById('disp-dyn-ret')?.value || '0');
        params.tip_touch = Boolean(document.getElementById('disp-tip-touch')?.checked);
    } else if (command === 'Mix') {
        params.volume = parseFloat(document.getElementById('mix-volume')?.value || '0');
        params.pre_aspirate = parseFloat(document.getElementById('mix-pre')?.value || '0');
        params.blowout = parseFloat(document.getElementById('mix-blowout')?.value || '0');
        params.liquid_class = document.getElementById('mix-liquid-class')?.value || '';
        params.pipette_technique = document.getElementById('mix-technique')?.value || '';
        params.mix_cycles = parseInt(document.getElementById('mix-cycles')?.value || '3', 10);
        params.dynamic_tip_extension = parseFloat(document.getElementById('mix-dyn-ext')?.value || '0');
        params.distance_from_bottom = parseFloat(document.getElementById('mix-asp-dist')?.value || '2');
        params.aspirate_distance = parseFloat(document.getElementById('mix-asp-dist')?.value || '2');
        params.dispense_at_different_distance = Boolean(document.getElementById('mix-different-disp')?.checked);
        params.dispense_distance = parseFloat(document.getElementById('mix-disp-dist')?.value || '2');
        params.tip_touch = Boolean(document.getElementById('mix-tip-touch')?.checked);
    } else if (command === 'Stack Plates') {
        params.base_location = parseInt(document.getElementById('stack-base-location')?.value || '0', 10);
        params.source_location = parseInt(document.getElementById('stack-source-location')?.value || '0', 10);
    } else if (command === 'Destack Plate') {
        params.source_location = parseInt(document.getElementById('destack-source-location')?.value || '0', 10);
        params.destination_location = parseInt(document.getElementById('destack-destination-location')?.value || '0', 10);
    } else if (command === 'Mount Plates') {
        // Same property shape as Stack — backend's bravo.mount_plates
        // also takes base_location + source_location; server.py
        // normalizes "Mount Plates" → cmd="mount_plates".
        params.base_location = parseInt(document.getElementById('mount-base-location')?.value || '0', 10);
        params.source_location = parseInt(document.getElementById('mount-source-location')?.value || '0', 10);
    } else if (command === 'Unmount Plate') {
        // Same property shape as Destack — backend's bravo.unmount_plate
        // takes source_location + destination_location.
        params.source_location = parseInt(document.getElementById('unmount-source-location')?.value || '0', 10);
        params.destination_location = parseInt(document.getElementById('unmount-destination-location')?.value || '0', 10);
    } else if (command === 'Delid Plate') {
        params.plate_location = parseInt(document.getElementById('delid-plate-location')?.value || '0', 10);
        params.lid_destination = parseInt(document.getElementById('delid-lid-destination')?.value || '0', 10);
    } else if (command === 'Relid Plate') {
        params.lid_location = parseInt(document.getElementById('relid-lid-location')?.value || '0', 10);
        params.plate_location = parseInt(document.getElementById('relid-plate-location')?.value || '0', 10);
    } else if (command === 'Scan Stack Height') {
        updateScanStackResultFields(null);
    } else if (command === 'Read Barcode') {
        { const el = document.getElementById('barcode-result'); if (el) el.textContent = '-'; }
    }

    if (command === 'Stack Plates') {
        log(`Executing ${command}: source ${params.source_location} -> base ${params.base_location}...`, 'info');
    } else if (command === 'Destack Plate') {
        log(`Executing ${command}: source ${params.source_location} -> empty pad ${params.destination_location}...`, 'info');
    } else if (command === 'Mount Plates') {
        log(`Executing ${command}: source ${params.source_location} -> base ${params.base_location} (locked pair)...`, 'info');
    } else if (command === 'Unmount Plate') {
        log(`Executing ${command}: mounted-pair ${params.source_location} -> empty pad ${params.destination_location}...`, 'info');
    } else if (command === 'Delid Plate') {
        log(`Executing ${command}: plate ${params.plate_location} -> lid destination ${params.lid_destination}...`, 'info');
    } else if (command === 'Relid Plate') {
        log(`Executing ${command}: lid ${params.lid_location} -> plate ${params.plate_location}...`, 'info');
    } else if (command === 'Scan Stack Height') {
        log(`Executing ${command} at location ${location}...`, 'info');
    } else {
        log(`Executing ${command} at location ${location}...`, 'info');
    }
    const res = await apiCall('/api/execute_command', 'POST', params);
    if (!res) {
        const collision = parseProcessCollisionError(state.lastApiError);
        if (collision) {
            renderProcessCollisionModal(collision);
        }
        return;
    }
    if (command === 'Scan Stack Height' && res?.status === 'manual_count_required') {
        const input = window.prompt(res.message || `No plate detected at location ${location}. Enter stacked plate count:`, '0');
        if (input !== null) {
            const manualCount = parseInt(String(input).trim(), 10);
            if (!Number.isNaN(manualCount) && manualCount >= 0) {
                const retryRes = await apiCall('/api/execute_command', 'POST', {
                    command,
                    location,
                    manual_count: manualCount,
                });
                if (retryRes?.status === 'completed') {
                    updateScanStackResultFields(retryRes);
                    log(retryRes.message || `${command} completed`, 'success');
                } else if (retryRes?.message) {
                    log(retryRes.message, 'error');
                }
                return;
            }
        }
    }
    if (res?.status === 'completed') {
        if (command === 'Scan Stack Height') {
            updateScanStackResultFields(res);
        } else if (command === 'Read Barcode') {
            { const el = document.getElementById('barcode-result'); if (el) el.textContent = res.barcode || 'No barcode returned'; }
        }
        log(res.message || `${command} completed`, 'success');
    } else if (res?.status === 'error') {
        if (command === 'Read Barcode') {
            { const el = document.getElementById('barcode-result'); if (el) el.textContent = res.message || 'Error'; }
        }
        log(res.message || `${command} failed`, 'error');
    } else if (res?.message) {
        log(res.message, 'error');
    }
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

function updateProcessCommandPanels(cmd) {
    const aspiratePanel = document.getElementById('cmd-params-aspirate');
    const dispensePanel = document.getElementById('cmd-params-dispense');
    const mixPanel = document.getElementById('cmd-params-mix');
    const stackPanel = document.getElementById('cmd-params-stack-plates');
    const destackPanel = document.getElementById('cmd-params-destack-plate');
    const mountPanel = document.getElementById('cmd-params-mount-plates');
    const unmountPanel = document.getElementById('cmd-params-unmount-plate');
    const delidPanel = document.getElementById('cmd-params-delid-plate');
    const relidPanel = document.getElementById('cmd-params-relid-plate');
    const scanPanel = document.getElementById('cmd-params-scan-stack');
    const barcodePanel = document.getElementById('cmd-params-read-barcode');
    if (aspiratePanel) aspiratePanel.style.display = cmd === 'Aspirate' ? 'block' : 'none';
    if (dispensePanel) dispensePanel.style.display = cmd === 'Dispense' ? 'block' : 'none';
    if (mixPanel) mixPanel.style.display = cmd === 'Mix' ? 'block' : 'none';
    if (stackPanel) stackPanel.style.display = cmd === 'Stack Plates' ? 'block' : 'none';
    if (destackPanel) destackPanel.style.display = cmd === 'Destack Plate' ? 'block' : 'none';
    if (mountPanel) mountPanel.style.display = cmd === 'Mount Plates' ? 'block' : 'none';
    if (unmountPanel) unmountPanel.style.display = cmd === 'Unmount Plate' ? 'block' : 'none';
    if (delidPanel) delidPanel.style.display = cmd === 'Delid Plate' ? 'block' : 'none';
    if (relidPanel) relidPanel.style.display = cmd === 'Relid Plate' ? 'block' : 'none';
    if (scanPanel) scanPanel.style.display = cmd === 'Scan Stack Height' ? 'block' : 'none';
    if (barcodePanel) barcodePanel.style.display = cmd === 'Read Barcode' ? 'block' : 'none';
    updateProcessPlateHandlingLabels();
}

// Show/hide command-specific parameter panels
document.getElementById('proc-command')?.addEventListener('change', (e) => {
    updateProcessCommandPanels(e.target.value);
});

updateProcessCommandPanels(document.getElementById('proc-command')?.value || 'Aspirate');

document.getElementById('proc-location')?.addEventListener('change', () => {
    syncProcessLabwareSelection();
});

for (const id of [
    'stack-base-location', 'stack-source-location',
    'destack-source-location', 'destack-destination-location',
    'mount-base-location', 'mount-source-location',
    'unmount-source-location', 'unmount-destination-location',
    'delid-plate-location', 'delid-lid-destination',
    'relid-lid-location', 'relid-plate-location',
]) {
    document.getElementById(id)?.addEventListener('change', updateProcessPlateHandlingLabels);
}

bindProcessNumericInputs();

const ACCESSORY_TYPE_LABELS = {
    barcode_reader: 'Barcode Reader',
    teleshake: 'Teleshake',
};

function accessoryTypeLabel(type) {
    return ACCESSORY_TYPE_LABELS[type] || String(type || 'Accessory').replace(/_/g, ' ');
}

function legacyBarcodeConfigured(br) {
    return Boolean(
        br
        && (
            br.enabled
            || Number(br.location || 0) > 0
            || (br.port && br.port !== 'COM5')
            || (br.device_type && br.device_type !== 'ms3')
            || (br.side && br.side !== 'east')
        )
    );
}

function legacyBarcodeToAccessory(br) {
    return {
        id: 'barcode_reader',
        type: 'barcode_reader',
        name: 'Barcode Reader',
        enabled: Boolean(br?.enabled),
        location: Number(br?.location || 0),
        holds_labware: true,
        connection: { kind: 'serial', port: br?.port || 'COM5' },
        settings: { device_type: br?.device_type || 'ms3', side: br?.side || 'east' },
    };
}

function nextAccessoryId(type) {
    const base = type === 'barcode_reader' ? 'barcode_reader' : (type || 'accessory');
    if (!state.accessoryDevices.some(item => item.id === base)) return base;
    let n = 2;
    while (state.accessoryDevices.some(item => item.id === `${base}_${n}`)) n += 1;
    return `${base}_${n}`;
}

function defaultAccessory(type) {
    const isBarcode = type === 'barcode_reader';
    return normalizeAccessoryDevice({
        id: nextAccessoryId(type),
        type,
        name: accessoryTypeLabel(type),
        enabled: true,
        location: 0,
        holds_labware: true,
        connection: { kind: 'serial', port: isBarcode ? 'COM5' : 'COM4' },
        settings: isBarcode
            ? { device_type: 'ms3', side: 'east' }
            : { default_rpm: 100, default_direction: 'NWSE', temperature_enabled: false },
        model: { path: type === 'teleshake' ? DEFAULT_TELESHAKE_MODEL_PATH : '' },
        teachpoint_hint: {},
    }, state.accessoryDevices.length);
}

function normalizeAccessoryDevice(raw, index = 0) {
    const type = String(raw?.type || 'barcode_reader');
    const connection = { ...(raw?.connection || {}) };
    const settings = { ...(raw?.settings || {}) };
    if (raw?.port && !connection.port) connection.port = raw.port;
    if (raw?.device_type && !settings.device_type) settings.device_type = raw.device_type;
    if (raw?.side && !settings.side) settings.side = raw.side;
    if (!connection.kind) connection.kind = 'serial';
    if (!connection.port) connection.port = type === 'barcode_reader' ? 'COM5' : 'COM4';
    if (type === 'barcode_reader') {
        if (!settings.device_type) settings.device_type = 'ms3';
        if (!settings.side) settings.side = 'east';
    } else if (type === 'teleshake') {
        settings.default_rpm = Number(settings.default_rpm || 100);
        settings.default_direction = settings.default_direction || 'NWSE';
        settings.temperature_enabled = Boolean(settings.temperature_enabled);
    }
    return {
        id: String(raw?.id || `${type}_${index + 1}`),
        type,
        name: String(raw?.name || accessoryTypeLabel(type)),
        enabled: raw?.enabled !== false,
        location: Number(raw?.location || 0),
        holds_labware: raw?.holds_labware !== false,
        connection,
        settings,
        model: { ...(raw?.model || {}) },
        teachpoint_hint: { ...(raw?.teachpoint_hint || {}) },
    };
}

function normalizeAccessoryDevices(accessories) {
    const rawDevices = Array.isArray(accessories?.devices) ? accessories.devices : [];
    let devices = rawDevices.map((item, index) => normalizeAccessoryDevice(item, index));
    if (!devices.length && legacyBarcodeConfigured(accessories?.barcode_reader)) {
        devices = [normalizeAccessoryDevice(legacyBarcodeToAccessory(accessories.barcode_reader), 0)];
    }
    return devices;
}

function selectedAccessory() {
    return state.accessoryDevices.find(item => item.id === state.selectedAccessoryId) || null;
}

function renderDeckAccessoryLabels() {
    for (let loc = 1; loc <= 9; loc += 1) {
        const el = document.querySelector(`.deck-cell[data-loc="${loc}"] .loc-accessory`);
        if (!el) continue;
        const names = state.accessoryDevices
            .filter(item => item.enabled && Number(item.location || 0) === loc)
            .map(item => item.name || accessoryTypeLabel(item.type));
        el.textContent = names.join(', ');
        el.title = names.join(', ');
    }
}

function scheduleDeckVisualRefresh() {
    if (accessoryVisualRefreshHandle) window.clearTimeout(accessoryVisualRefreshHandle);
    accessoryVisualRefreshHandle = window.setTimeout(() => {
        accessoryVisualRefreshHandle = null;
        void refreshDeckLabwareScene();
    }, 120);
}

function renderAccessoryList() {
    const list = document.getElementById('accessory-list');
    if (!list) return;
    list.replaceChildren();
    if (!state.accessoryDevices.length) {
        const empty = document.createElement('div');
        empty.className = 'text-xs text-dim';
        empty.textContent = 'No accessories configured.';
        list.appendChild(empty);
    }
    if (!state.selectedAccessoryId && state.accessoryDevices.length) {
        state.selectedAccessoryId = state.accessoryDevices[0].id;
    }
    for (const device of state.accessoryDevices) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `accessory-row${device.id === state.selectedAccessoryId ? ' selected' : ''}`;
        row.dataset.accessoryId = device.id;

        const enabled = document.createElement('span');
        enabled.textContent = device.enabled ? 'ON' : 'OFF';
        enabled.className = 'accessory-meta';
        row.appendChild(enabled);

        const name = document.createElement('span');
        name.className = 'accessory-name';
        name.textContent = device.name || accessoryTypeLabel(device.type);
        row.appendChild(name);

        const type = document.createElement('span');
        type.className = 'accessory-meta';
        type.textContent = accessoryTypeLabel(device.type);
        row.appendChild(type);

        const loc = document.createElement('span');
        loc.className = 'accessory-meta';
        loc.textContent = Number(device.location || 0) > 0 ? `Loc ${device.location}` : 'No loc';
        row.appendChild(loc);

        row.addEventListener('click', () => {
            saveAccessoryEditorToState();
            state.selectedAccessoryId = device.id;
            renderAccessoryList();
        });
        list.appendChild(row);
    }
    populateAccessoryEditor(selectedAccessory());
    renderDeckAccessoryLabels();
    scheduleDeckVisualRefresh();
}

function updateAccessoryTypePanels(type) {
    document.querySelectorAll('.accessory-type-panel').forEach(panel => panel.classList.remove('active'));
    const panel = document.getElementById(type === 'teleshake' ? 'accessory-panel-teleshake' : 'accessory-panel-barcode');
    panel?.classList.add('active');
}

function populateAccessoryEditor(device) {
    const editor = document.getElementById('accessory-editor');
    if (!editor) return;
    editor.style.display = device ? 'block' : 'none';
    if (!device) return;
    setCheck('prof-accessory-enabled', device.enabled);
    setInput('prof-accessory-name', device.name || accessoryTypeLabel(device.type));
    setInput('prof-accessory-id', device.id);
    const typeSel = document.getElementById('prof-accessory-type');
    if (typeSel) typeSel.value = device.type || 'barcode_reader';
    const locSel = document.getElementById('prof-accessory-location');
    if (locSel) locSel.value = String(device.location || 0);
    setInput('prof-accessory-port', device.connection?.port || (device.type === 'barcode_reader' ? 'COM5' : 'COM4'));
    setCheck('prof-accessory-holds-labware', device.holds_labware !== false);
    setInput('prof-accessory-model-path', accessoryModelPath(device));
    setInput('prof-accessory-z-hint', device.teachpoint_hint?.z_delta_mm ?? 0);

    const scannerType = document.getElementById('prof-accessory-barcode-device-type');
    if (scannerType) scannerType.value = device.settings?.device_type || 'ms3';
    const side = document.getElementById('prof-accessory-barcode-side');
    if (side) side.value = device.settings?.side || 'east';
    setInput('prof-accessory-teleshake-rpm', device.settings?.default_rpm || 100);
    const direction = document.getElementById('prof-accessory-teleshake-direction');
    if (direction) direction.value = device.settings?.default_direction || 'NWSE';
    setCheck('prof-accessory-teleshake-temperature', Boolean(device.settings?.temperature_enabled));
    updateAccessoryTypePanels(device.type);
}

function readAccessoryEditor() {
    const current = selectedAccessory();
    if (!current) return null;
    const type = document.getElementById('prof-accessory-type')?.value || 'barcode_reader';
    const id = (document.getElementById('prof-accessory-id')?.value || current.id || nextAccessoryId(type)).trim();
    const settings = {};
    if (type === 'barcode_reader') {
        settings.device_type = document.getElementById('prof-accessory-barcode-device-type')?.value || 'ms3';
        settings.side = document.getElementById('prof-accessory-barcode-side')?.value || 'east';
    } else if (type === 'teleshake') {
        settings.default_rpm = parseInt(document.getElementById('prof-accessory-teleshake-rpm')?.value || '100', 10);
        settings.default_direction = document.getElementById('prof-accessory-teleshake-direction')?.value || 'NWSE';
        settings.temperature_enabled = document.getElementById('prof-accessory-teleshake-temperature')?.checked ?? false;
    }
    const modelPath = (document.getElementById('prof-accessory-model-path')?.value || '').trim();
    const zHint = parseFloat(document.getElementById('prof-accessory-z-hint')?.value || '0');
    return normalizeAccessoryDevice({
        ...current,
        id,
        type,
        name: (document.getElementById('prof-accessory-name')?.value || accessoryTypeLabel(type)).trim(),
        enabled: document.getElementById('prof-accessory-enabled')?.checked ?? true,
        location: parseInt(document.getElementById('prof-accessory-location')?.value || '0', 10),
        holds_labware: document.getElementById('prof-accessory-holds-labware')?.checked ?? true,
        connection: {
            kind: 'serial',
            port: (document.getElementById('prof-accessory-port')?.value || (type === 'barcode_reader' ? 'COM5' : 'COM4')).trim(),
        },
        settings,
        model: modelPath ? { path: modelPath } : {},
        teachpoint_hint: Number.isFinite(zHint) && zHint !== 0 ? { z_delta_mm: zHint, requires_validation: true } : {},
    }, 0);
}

function saveAccessoryEditorToState() {
    const edited = readAccessoryEditor();
    if (!edited) return;
    const oldId = state.selectedAccessoryId;
    const index = state.accessoryDevices.findIndex(item => item.id === oldId);
    if (index >= 0) {
        state.accessoryDevices[index] = edited;
        state.selectedAccessoryId = edited.id;
    }
}

function collectAccessoryDevicesFromUi() {
    saveAccessoryEditorToState();
    return state.accessoryDevices.map((device, index) => normalizeAccessoryDevice(device, index));
}

function setTeleshakeStatus(text) {
    const el = document.getElementById('accessory-teleshake-status');
    if (el) el.textContent = text || '-';
}

async function syncAccessoriesToBackend() {
    const devices = collectAccessoryDevicesFromUi();
    const res = await apiCall('/api/profile', 'PATCH', {
        accessories: { devices },
    });
    return Boolean(res);
}

async function runSelectedTeleshakeAction(action) {
    const device = selectedAccessory();
    if (!device || device.type !== 'teleshake') {
        log('Select a Teleshake accessory first', 'error');
        return;
    }
    if (!device.enabled) {
        log('Enable the Teleshake accessory before running it', 'error');
        return;
    }
    setTeleshakeStatus(action === 'start' ? 'starting...' : 'stopping...');
    const synced = await syncAccessoriesToBackend();
    if (!synced) {
        setTeleshakeStatus('sync failed');
        return;
    }

    const current = selectedAccessory() || device;
    const body = action === 'start'
        ? {
            rpm: parseInt(document.getElementById('prof-accessory-teleshake-rpm')?.value || current.settings?.default_rpm || '100', 10),
            direction: document.getElementById('prof-accessory-teleshake-direction')?.value || current.settings?.default_direction || 'NWSE',
        }
        : {};
    const endpoint = `/api/accessories/${encodeURIComponent(current.id)}/teleshake/${action}`;
    const res = await apiCall(endpoint, 'POST', body);
    if (res) {
        setTeleshakeStatus(res.status || action);
        log(`Teleshake ${res.status || action}`, 'success');
    } else {
        setTeleshakeStatus('error');
    }
}

// ══════════════════════════════════════════════════════════════════════
// PROFILES TAB
// ══════════════════════════════════════════════════════════════════════

async function loadProfile() {
    // Populate profile selector with available profiles from server
    const profilesRes = await apiCall('/api/profiles', 'GET');
    const select = document.getElementById('prof-select');
    if (profilesRes && select) {
        const prev = select.value;
        select.options.length = 0;
        for (const name of (profilesRes.profiles || [])) {
            select.add(new Option(name, name));
        }
        const target = (prev && profilesRes.profiles.includes(prev)) ? prev : profilesRes.current;
        if (target) select.value = target;
        const activeEl = document.getElementById('prof-active-name');
        if (activeEl) activeEl.textContent = profilesRes.current || '—';
        // Teaching writes into the active profile, so the teach dialog needs
        // its name to show the operator what they are about to modify.
        state.activeProfileName = profilesRes.current || '';
    }

    // Fetch current profile settings and populate form
    const res = await apiCall('/api/profile', 'GET');
    if (!res) return;
    state.profileLoaded = true;

    if (res.gripper && typeof res.gripper.y_offset === 'number') {
        state.profileGripperYOffset = res.gripper.y_offset;
    }
    refreshGripperTeachPanel();

    if (res.connection) {
        const c = res.connection;
        const ctrlSel = document.getElementById('conn-controller-type');
        if (ctrlSel) {
            ctrlSel.value = c.controller_type || 'simulation';
            updateConnectionFields(c.controller_type);
        }
        state.machineId = c.machine_id || state.machineId || 'SIM_BRAVO';
        setInput('conn-machine-id', state.machineId);
        setInput('conn-address', c.address || '');
        setInput('conn-serial-port', c.serial_port || '');
        updateConnectButton(false);
    }

    if (res.vision) {
        state.visionEnabled = Boolean(res.vision.enabled);
        state.visionServiceUrl = res.vision.service_url || state.visionServiceUrl;
        state.visionSdkRoot = res.vision.sdk_root || state.visionSdkRoot;
        setCheck('prof-vision-enabled', state.visionEnabled);
        setInput('prof-vision-service-url', state.visionServiceUrl);
        setInput('prof-vision-sdk-root', state.visionSdkRoot);
    } else {
        state.visionEnabled = false;
        setCheck('prof-vision-enabled', false);
    }
    updateVisionUiVisibility();

    state.accessoryDevices = normalizeAccessoryDevices(res.accessories || {});
    state.selectedAccessoryId = state.accessoryDevices[0]?.id || '';
    renderAccessoryList();

    if (res.safety) {
        const s = res.safety;
        setInput('prof-approach', s.approach_height);
        setInput('prof-safe-z', s.z_safe_position);
        setCheck('prof-home-w', s.prompt_home_w);
        setCheck('prof-med-speed', s.run_medium_speed);
        setCheck('prof-safe-z-always', s.always_move_to_safe_z);
        setCheck('prof-ignore-plate', s.ignore_plate_sensor);
        setCheck('prof-tip-touch', s.enable_tips_off_tip_touch);
        setCheck('prof-srt', s.is_srt);
    }

    if (res.head) {
        setCheck('prof-check-head', res.head.check_on_init);
        const headSel = document.getElementById('prof-head-type');
        if (headSel && res.head.head_type) headSel.value = res.head.head_type;
        await loadTipDefinitions(res.head.head_type);
        populateTeachTipOptions(
            res.head.head_type,
            res.head.teach_tip_id || res.head.default_tip_id || res.head.teach_tip_capacity || res.head.default_tip_capacity,
            res.head.teach_tip_options,
        );
        state.activeTipId = res.head.teach_tip_id || res.head.default_tip_id || state.activeTipId;
        state.activeTipCapacityUl = Number(res.head.teach_tip_capacity || res.head.default_tip_capacity || 0);
    }

    await loadHeadMode();
    await loadLiquidClasses();
    await loadPipetteTechniques();
    refreshTipboxAssignmentControls();
}

async function loadHeadMode() {
    const res = await apiCall('/api/head_mode', 'GET');
    if (!res) return;
    state.headType = res.head_type || state.headType;
    state.headMode = res.head_mode || state.headMode;
    const procHeadMode = document.getElementById('proc-head-mode');
    if (procHeadMode) procHeadMode.textContent = describeHeadMode(state.headMode);
}

document.getElementById('btn-load-profile')?.addEventListener('click', async () => {
    const name = document.getElementById('prof-select')?.value;
    if (!name) { log('No profile selected', 'error'); return; }
    log(`Loading profile "${name}"...`, 'info');
    const res = await apiCall('/api/profile/load', 'POST', { name });
    if (res?.status === 'loaded') {
        log(`Profile "${name}" loaded`, 'success');
        await loadProfile();
    } else {
        log(`Failed to load profile: ${res?.detail || 'unknown error'}`, 'error');
    }
});

function setInput(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.value = val;
}

function setCheck(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.checked = val;
}

document.getElementById('btn-accessory-add-barcode')?.addEventListener('click', async () => {
    saveAccessoryEditorToState();
    const device = defaultAccessory('barcode_reader');
    const previous = state.accessoryDevices;
    state.accessoryDevices = [...state.accessoryDevices, device];
    state.selectedAccessoryId = device.id;
    renderAccessoryList();

    // Persist immediately so the list on screen always matches the profile —
    // the same reason Remove does. Field edits below still go through
    // "Save Settings".
    if (!await syncAccessoriesToBackend()) {
        state.accessoryDevices = previous;
        state.selectedAccessoryId = previous[0]?.id || '';
        renderAccessoryList();
        log(`Could not add accessory: ${state.lastApiError || 'save failed'}`, 'error');
    }
});

document.getElementById('btn-accessory-add-teleshake')?.addEventListener('click', async () => {
    saveAccessoryEditorToState();
    const device = defaultAccessory('teleshake');
    const previous = state.accessoryDevices;
    state.accessoryDevices = [...state.accessoryDevices, device];
    state.selectedAccessoryId = device.id;
    renderAccessoryList();

    // Persist immediately so the list on screen always matches the profile —
    // the same reason Remove does. Field edits below still go through
    // "Save Settings".
    if (!await syncAccessoriesToBackend()) {
        state.accessoryDevices = previous;
        state.selectedAccessoryId = previous[0]?.id || '';
        renderAccessoryList();
        log(`Could not add accessory: ${state.lastApiError || 'save failed'}`, 'error');
    }
});

document.getElementById('btn-accessory-remove')?.addEventListener('click', async () => {
    const selected = selectedAccessory();
    if (!selected) return;
    const label = selected.name || selected.id;
    if (!window.confirm(
        `Remove "${label}" from profile ${state.activeProfileName || '(active)'}?\n\n`
        + 'This is saved to the profile immediately.'
    )) return;

    // Persist straight away. Staging the removal and relying on Save Settings
    // meant navigating away silently brought the accessory back.
    const previous = state.accessoryDevices;
    const previousSelection = state.selectedAccessoryId;
    state.accessoryDevices = state.accessoryDevices.filter(item => item.id !== selected.id);
    state.selectedAccessoryId = state.accessoryDevices[0]?.id || '';
    renderAccessoryList();

    if (await syncAccessoriesToBackend()) {
        log(`Removed accessory "${label}" from the profile`, 'success');
        return;
    }
    // Save failed — put it back rather than showing a list the server disagrees with.
    state.accessoryDevices = previous;
    state.selectedAccessoryId = previousSelection;
    renderAccessoryList();
    log(`Could not remove "${label}": ${state.lastApiError || 'save failed'}`, 'error');
});

document.getElementById('btn-accessory-teleshake-start')?.addEventListener('click', async () => {
    await runSelectedTeleshakeAction('start');
});

document.getElementById('btn-accessory-teleshake-stop')?.addEventListener('click', async () => {
    await runSelectedTeleshakeAction('stop');
});

[
    'prof-accessory-enabled',
    'prof-accessory-name',
    'prof-accessory-id',
    'prof-accessory-type',
    'prof-accessory-location',
    'prof-accessory-port',
    'prof-accessory-holds-labware',
    'prof-accessory-barcode-device-type',
    'prof-accessory-barcode-side',
    'prof-accessory-teleshake-rpm',
    'prof-accessory-teleshake-direction',
    'prof-accessory-teleshake-temperature',
    'prof-accessory-model-path',
    'prof-accessory-z-hint',
].forEach((id) => {
    const el = document.getElementById(id);
    el?.addEventListener('change', () => {
        saveAccessoryEditorToState();
        renderAccessoryList();
    });
    el?.addEventListener('input', () => {
        saveAccessoryEditorToState();
        renderDeckAccessoryLabels();
        scheduleDeckVisualRefresh();
    });
});

document.getElementById('btn-update-profile')?.addEventListener('click', async () => {
    const params = {
        approach_height: parseFloat(document.getElementById('prof-approach')?.value || '10'),
        z_safe_position: parseFloat(document.getElementById('prof-safe-z')?.value || '0'),
        prompt_home_w: document.getElementById('prof-home-w')?.checked ?? true,
        run_medium_speed: document.getElementById('prof-med-speed')?.checked ?? false,
        always_safe_z: document.getElementById('prof-safe-z-always')?.checked ?? true,
        ignore_plate_sensor: document.getElementById('prof-ignore-plate')?.checked ?? false,
        enable_tips_off_tip_touch: document.getElementById('prof-tip-touch')?.checked ?? true,
        is_srt: document.getElementById('prof-srt')?.checked ?? false,
        controller_type: document.getElementById('conn-controller-type')?.value || 'simulation',
        use_ethernet: document.getElementById('conn-controller-type')?.value !== 'darwin_serial',
        machine_id: document.getElementById('conn-machine-id')?.value || 'SIM_BRAVO',
        address: document.getElementById('conn-address')?.value || '',
        serial_port: document.getElementById('conn-serial-port')?.value || '',
        head_type: document.getElementById('prof-head-type')?.value || null,
        check_on_init: document.getElementById('prof-check-head')?.checked ?? true,
        teach_tip_id: state.activeTipId || document.getElementById('tp-tip-capacity')?.value || null,
        vision_enabled: document.getElementById('prof-vision-enabled')?.checked ?? false,
        vision_service_url: document.getElementById('prof-vision-service-url')?.value || 'http://127.0.0.1:8101',
        vision_sdk_root: document.getElementById('prof-vision-sdk-root')?.value || 'external/pyorbbecsdk',
        accessories: {
            devices: collectAccessoryDevicesFromUi(),
        },
    };
    log('Saving profile settings...', 'info');
    const res = await apiCall('/api/profile', 'PATCH', params);
    if (res) {
        state.visionEnabled = params.vision_enabled;
        state.visionServiceUrl = params.vision_service_url;
        state.visionSdkRoot = params.vision_sdk_root;
        updateVisionUiVisibility();
        log(res.saved ? 'Profile saved to disk' : 'Profile updated (not saved)', 'success');
    }
});

document.getElementById('btn-refresh-profiles')?.addEventListener('click', async () => {
    log('Refreshing profile list...', 'info');
    await loadProfile();
    log('Profile list refreshed', 'success');
});

document.getElementById('btn-duplicate-profile')?.addEventListener('click', async () => {
    const source = document.getElementById('prof-select')?.value;
    if (!source) { log('No profile selected to duplicate', 'error'); return; }
    const newName = prompt(`Duplicate "${source}" as:`, `${source}_copy`);
    if (!newName) return;
    const trimmed = newName.trim();
    if (!trimmed) { log('Empty profile name', 'error'); return; }
    log(`Duplicating "${source}" -> "${trimmed}"...`, 'info');
    const res = await apiCall('/api/profile/duplicate', 'POST', { source, new_name: trimmed });
    if (res?.status === 'duplicated') {
        log(`Profile duplicated as "${res.name}"`, 'success');
        await loadProfile();
    } else {
        log(`Failed to duplicate profile: ${res?.detail || 'unknown error'}`, 'error');
    }
});

document.getElementById('btn-rename-profile')?.addEventListener('click', async () => {
    const oldName = document.getElementById('prof-select')?.value;
    if (!oldName) { log('No profile selected to rename', 'error'); return; }
    const newName = prompt(`Rename "${oldName}" to:`, oldName);
    if (!newName) return;
    const trimmed = newName.trim();
    if (!trimmed || trimmed === oldName) return;
    log(`Renaming "${oldName}" -> "${trimmed}"...`, 'info');
    const res = await apiCall('/api/profile/rename', 'POST', { old_name: oldName, new_name: trimmed });
    if (res?.status === 'renamed') {
        log(`Profile renamed to "${res.new_name}"${res.was_active ? ' (active updated)' : ''}`, 'success');
        await loadProfile();
    } else {
        log(`Failed to rename profile: ${res?.detail || 'unknown error'}`, 'error');
    }
});

function _readFileAsText(file) {
    // Legacy .reg exports are UTF-16LE with a BOM; FileReader.readAsText
    // honors the BOM when no explicit encoding is given.
    return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(r.result);
        r.onerror = () => reject(r.error);
        r.readAsText(file);
    });
}

document.getElementById('btn-import-reg-profile')?.addEventListener('click', () => {
    document.getElementById('prof-reg-file')?.click();
});

document.getElementById('btn-import-dat-profile')?.addEventListener('click', () => {
    document.getElementById('prof-dat-dir')?.click();
});

document.getElementById('prof-reg-file')?.addEventListener('change', async (event) => {
    const input = event.target;
    const file = input?.files?.[0];
    input.value = '';  // allow re-importing the same file
    if (!file) return;
    log(`Reading ${file.name}...`, 'info');
    let content;
    try {
        content = await _readFileAsText(file);
    } catch (err) {
        log(`Failed to read file: ${err?.message || err}`, 'error');
        return;
    }
    const preview = await apiCall('/api/profile/import_reg', 'POST', { content });
    if (!preview || preview.status !== 'previewed') {
        log(`Could not parse .reg: ${preview?.detail || 'unknown error'}`, 'error');
        return;
    }
    const defaultName = preview.parsed_name || file.name.replace(/\.reg$/i, '');
    const targetName = prompt(
        `Save imported profile as:\n\n` +
        `Parsed name: ${preview.parsed_name}\n` +
        `Axes: ${(preview.axes || []).join(', ')}\n` +
        `Teachpoints: ${(preview.teachpoint_locations || []).join(', ')}\n` +
        (preview.warnings?.length ? `\nWarnings:\n - ${preview.warnings.join('\n - ')}\n` : ''),
        defaultName,
    );
    if (!targetName) return;
    const saveAs = targetName.trim();
    if (!saveAs) return;
    let res = await apiCall('/api/profile/import_reg', 'POST', { content, save_as: saveAs });
    if (res?.status !== 'saved' && /already exists/i.test(res?.detail || '')) {
        if (!confirm(`Profile "${saveAs}" already exists. Overwrite?`)) return;
        res = await apiCall('/api/profile/import_reg', 'POST', { content, save_as: saveAs, overwrite: true });
    }
    if (res?.status === 'saved') {
        log(`Imported profile "${res.name}" (${(res.warnings || []).length} warnings)`, 'success');
        for (const w of res.warnings || []) log(`Warning: ${w}`, 'info');
        await loadProfile();
    } else {
        log(`Failed to import profile: ${res?.detail || 'unknown error'}`, 'error');
    }
});

document.getElementById('prof-dat-dir')?.addEventListener('change', async (event) => {
    const input = event.target;
    const picked = Array.from(input?.files || []);
    input.value = '';  // allow re-importing the same folder
    if (!picked.length) return;
    // Only keep the .dat files; other stragglers (README, screenshots, etc.) are ignored.
    const datFiles = picked.filter(f => /\.dat$/i.test(f.name));
    if (!datFiles.length) {
        log('Selected folder contains no .dat files', 'error');
        return;
    }
    // Infer the profile name from the top-level folder of the selection.
    const firstPath = datFiles[0].webkitRelativePath || datFiles[0].name;
    const topFolder = firstPath.split('/')[0];
    if (!topFolder) {
        log('Could not determine profile name from folder selection', 'error');
        return;
    }
    log(`Reading ${datFiles.length} .dat files from "${topFolder}"...`, 'info');
    const files = [];
    try {
        for (const f of datFiles) {
            const content = await _readFileAsText(f);
            files.push({ relative_path: f.webkitRelativePath || f.name, content });
        }
    } catch (err) {
        log(`Failed to read .dat files: ${err?.message || err}`, 'error');
        return;
    }
    const preview = await apiCall('/api/profile/import_dat', 'POST', {
        profile_name: topFolder,
        files,
    });
    if (!preview || preview.status !== 'previewed') {
        log(`Could not parse .dat tree: ${preview?.detail || 'unknown error'}`, 'error');
        return;
    }
    const defaultName = preview.parsed_name || topFolder;
    const targetName = prompt(
        `Save imported profile as:\n\n` +
        `Parsed name: ${preview.parsed_name}\n` +
        `Axes: ${(preview.axes || []).join(', ')}\n` +
        `Teachpoints: ${(preview.teachpoint_locations || []).join(', ')}\n` +
        (preview.warnings?.length ? `\nWarnings:\n - ${preview.warnings.join('\n - ')}\n` : ''),
        defaultName,
    );
    if (!targetName) return;
    const saveAs = targetName.trim();
    if (!saveAs) return;
    let res = await apiCall('/api/profile/import_dat', 'POST', {
        profile_name: topFolder,
        files,
        save_as: saveAs,
    });
    if (res?.status !== 'saved' && /already exists/i.test(res?.detail || '')) {
        if (!confirm(`Profile "${saveAs}" already exists. Overwrite?`)) return;
        res = await apiCall('/api/profile/import_dat', 'POST', {
            profile_name: topFolder,
            files,
            save_as: saveAs,
            overwrite: true,
        });
    }
    if (res?.status === 'saved') {
        log(`Imported profile "${res.name}" (${(res.warnings || []).length} warnings)`, 'success');
        for (const w of res.warnings || []) log(`Warning: ${w}`, 'info');
        await loadProfile();
    } else {
        log(`Failed to import profile: ${res?.detail || 'unknown error'}`, 'error');
    }
});

document.getElementById('prof-vision-enabled')?.addEventListener('change', (event) => {
    state.visionEnabled = Boolean(event.target?.checked);
    updateVisionUiVisibility();
});

document.getElementById('btn-reinit-profile')?.addEventListener('click', async () => {
    if (state.commandRunning) return;
    state.commandRunning = true;
    state.commandRunningAt = Date.now();
    setMotionButtonsEnabled(false);
    try {
        log('Reinitializing...', 'info');
        const res = await apiCall('/api/initialize');
        if (res) log('Reinitialized', 'success');
    } finally {
        state.commandRunning = false;
        setMotionButtonsEnabled(true);
    }
});

// ══════════════════════════════════════════════════════════════════════
// CHANGE HEAD WIZARD
// ══════════════════════════════════════════════════════════════════════

{
    const overlay = document.getElementById('modal-change-head');
    const steps = overlay.querySelectorAll('.wizard-step');
    const btnBack = document.getElementById('chw-back');
    const btnNext = document.getElementById('chw-next');
    const btnFinish = document.getElementById('chw-finish');
    const btnCancel = document.getElementById('chw-cancel');
    const btnClose = document.getElementById('chw-close');
    let currentStep = 1;

    function showStep(n) {
        currentStep = n;
        steps.forEach(s => s.classList.toggle('active', parseInt(s.dataset.step) === n));
        btnBack.style.display = n > 1 ? '' : 'none';
        btnNext.style.display = n < steps.length ? '' : 'none';
        btnFinish.style.display = n === steps.length ? '' : 'none';
    }

    function openWizard() {
        showStep(1);
        overlay.classList.add('open');
    }

    function closeWizard() {
        overlay.classList.remove('open');
    }

    document.getElementById('btn-change-head')?.addEventListener('click', openWizard);
    btnClose.addEventListener('click', closeWizard);
    btnCancel.addEventListener('click', closeWizard);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeWizard(); });

    btnNext.addEventListener('click', () => {
        if (currentStep < steps.length) showStep(currentStep + 1);
    });

    btnBack.addEventListener('click', () => {
        if (currentStep > 1) showStep(currentStep - 1);
    });

    btnFinish.addEventListener('click', async () => {
        const headType = document.getElementById('chw-head-select').value;
        log(`Changing head to ${headType}...`, 'info');
        const res = await apiCall('/api/change_head', 'POST', { head_type: headType });
        if (res) {
            log(`Head changed to ${res.head_type_display || headType}`, 'success');
            const profHead = document.getElementById('prof-head-type');
            if (profHead) profHead.value = headType;
            await loadHeadMode();
        }
        closeWizard();
    });
}

// ══════════════════════════════════════════════════════════════════════
// FIND DEVICE DIALOG
// ══════════════════════════════════════════════════════════════════════

{
    const overlay = document.getElementById('modal-find-device');
    const tbody = document.getElementById('fd-device-list');
    const btnOk = document.getElementById('fd-ok');
    const btnRefresh = document.getElementById('fd-refresh');
    const btnCancel = document.getElementById('fd-cancel');
    const btnClose = document.getElementById('fd-close');
    const adapterSelect = document.getElementById('fd-adapter');
    let selectedDeviceId = null;
    let selectedDeviceIp = null;
    let selectedControllerType = null;

    function openDialog() {
        selectedDeviceId = null;
        selectedDeviceIp = null;
        selectedControllerType = null;
        btnOk.disabled = true;
        overlay.classList.add('open');
        refreshDevices();
    }

    function closeDialog() {
        overlay.classList.remove('open');
    }

    document.getElementById('btn-find-device')?.addEventListener('click', openDialog);
    btnClose.addEventListener('click', closeDialog);
    btnCancel.addEventListener('click', closeDialog);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeDialog(); });

    function renderDevices(devices) {
        tbody.options && (tbody.options.length = 0);
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

        if (!devices || devices.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 5;
            td.style.cssText = 'text-align:center;color:var(--text-dimmer);padding:20px';
            td.textContent = 'No devices found on the network.';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        devices.forEach(d => {
            const isSim = d.status === 'Simulation';
            const tr = document.createElement('tr');
            if (isSim) tr.style.opacity = '0.6';

            [d.device_id, d.device_type, d.ip_address, d.mac_address].forEach(val => {
                const td = document.createElement('td');
                td.textContent = String(val ?? '—');
                tr.appendChild(td);
            });

            const statusTd = document.createElement('td');
            statusTd.textContent = d.status;
            if (isSim) statusTd.style.color = 'var(--text-dimmer)';
            else if (d.status === 'Matched') statusTd.style.color = 'var(--green)';
            tr.appendChild(statusTd);

            if (d.device_id === selectedDeviceId) tr.classList.add('selected');

            if (!isSim) {
                tr.addEventListener('click', () => {
                    selectedDeviceId = d.device_id;
                    selectedDeviceIp = d.ip_address;
                    selectedControllerType = d.device_type === 'DARWIN' ? 'darwin_native' : 'agile';
                    btnOk.disabled = false;
                    tbody.querySelectorAll('tr').forEach(r => r.classList.remove('selected'));
                    tr.classList.add('selected');
                });
            }
            tbody.appendChild(tr);
        });
    }

    function renderAdapters(adapters) {
        adapterSelect.innerHTML = '';
        // Always include "All interfaces" as the first option
        const allOpt = document.createElement('option');
        allOpt.value = 'All interfaces';
        allOpt.textContent = 'All interfaces';
        adapterSelect.appendChild(allOpt);

        if (adapters && adapters.length > 0) {
            adapters.forEach(a => {
                const opt = document.createElement('option');
                opt.value = a.ip;
                opt.textContent = `${a.name} — ${a.ip}`;
                adapterSelect.appendChild(opt);
            });
        }
    }

    async function refreshDevices() {
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        const scanTr = document.createElement('tr');
        const scanTd = document.createElement('td');
        scanTd.colSpan = 5;
        scanTd.style.cssText = 'text-align:center;color:var(--text-dimmer);padding:20px';
        scanTd.textContent = 'Scanning…';
        scanTr.appendChild(scanTd);
        tbody.appendChild(scanTr);
        const controllerType = document.getElementById('conn-controller-type')?.value || undefined;
        const res = await apiCall('/api/discover_devices', 'POST', {
            adapter: adapterSelect.value,
            controller_type: controllerType,
        });
        if (res) {
            renderDevices(res.devices);
            if (res.adapters) renderAdapters(res.adapters);
            return;
        }
        // apiCall swallows failures and returns null. Without this the
        // "Scanning…" row stays up forever and the scan looks like it hung.
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        const errTr = document.createElement('tr');
        const errTd = document.createElement('td');
        errTd.colSpan = 5;
        errTd.style.cssText = 'text-align:center;color:var(--danger,#c66);padding:20px';
        errTd.textContent = state.lastApiError
            ? `Scan failed: ${state.lastApiError}`
            : 'Scan failed. See the log for details.';
        errTr.appendChild(errTd);
        tbody.appendChild(errTr);
    }

    btnRefresh.addEventListener('click', refreshDevices);

    btnOk.addEventListener('click', async () => {
        if (selectedDeviceId === null) return;
        log(`Selecting device: ID=${selectedDeviceId} IP=${selectedDeviceIp}`, 'info');
        const res = await apiCall('/api/select_device', 'POST', {
            device_id: String(selectedDeviceId),
            ip_address: selectedDeviceIp || '',
            controller_type: selectedControllerType,
        });
        if (res) {
            log(`Device selected. IP: ${res.ip_address}`, 'success');
            // Populate the address field so Connect uses this IP
            const addrInput = document.getElementById('conn-address');
            if (addrInput && res.ip_address) addrInput.value = res.ip_address;
            const ctrlSel = document.getElementById('conn-controller-type');
            if (ctrlSel && res.controller_type) {
                ctrlSel.value = res.controller_type;
                updateConnectionFields(res.controller_type);
                updateConnectButton(false);
            }
        }
        closeDialog();
    });
}

{
    const overlay = document.getElementById('modal-head-mode');
    const btnOpen = document.getElementById('btn-set-head-mode');
    const btnClose = document.getElementById('hmd-close');
    const btnCancel = document.getElementById('hmd-cancel');
    const btnSave = document.getElementById('hmd-save');
    const btnSuggest = document.getElementById('hmd-suggest');
    const btnReset = document.getElementById('hmd-reset');
    const subsetType = document.getElementById('hmd-subset-type');
    const subsetConfig = document.getElementById('hmd-subset-config');
    const countRow = document.getElementById('hmd-count-row');
    const countInput = document.getElementById('hmd-count');
    const dragHint = document.getElementById('hmd-drag-hint');
    const grid = document.getElementById('hmd-grid');
    const summary = document.getElementById('hmd-summary');
    let draftMode = null;
    let rectangleDragActive = false;

    function rectangleCountsFromCell(geometry, subsetConfigValue, modelRow, col) {
        const front = String(subsetConfigValue || 'front_left').startsWith('front');
        const left = String(subsetConfigValue || 'front_left').endsWith('left');
        return {
            row_count: front ? (modelRow + 1) : (geometry.rows - modelRow),
            column_count: left ? (col + 1) : (geometry.columns - col),
        };
    }

    function updateCountControl() {
        const headType = state.headType || document.getElementById('prof-head-type')?.value || 'HT_96_D_70';
        const geometry = getHeadGeometry(headType);
        const subset = subsetType.value;
        const usesCount = subset === 'row' || subset === 'column';
        if (dragHint) dragHint.style.display = subset === 'rectangle' ? 'block' : 'none';
        if (countRow) countRow.style.display = usesCount ? 'flex' : 'none';
        if (!countInput) return;
        if (subset === 'row') {
            countInput.min = '1';
            countInput.max = String(geometry.rows);
            countInput.value = String(Math.max(1, Math.min(geometry.rows, Number(draftMode?.row_count || 1))));
        } else if (subset === 'column') {
            countInput.min = '1';
            countInput.max = String(geometry.columns);
            countInput.value = String(Math.max(1, Math.min(geometry.columns, Number(draftMode?.column_count || 1))));
        }
    }

    function renderHeadModeGrid() {
        const headType = state.headType || document.getElementById('prof-head-type')?.value || 'HT_96_D_70';
        draftMode = normalizeHeadModeForUi(headType, draftMode);
        updateCountControl();
        const { geometry, normalized, selected, rowStart, colStart } = selectedHeadCells(headType, draftMode);
        const anchor = headModeAnchorCell(geometry, normalized, rowStart, colStart);
        grid.style.gridTemplateColumns = `repeat(${geometry.columns}, 12px)`;
        grid.innerHTML = '';
        for (let displayRow = 0; displayRow < geometry.rows; displayRow++) {
            const modelRow = displayRowToModelRow(geometry, displayRow);
            for (let col = 0; col < geometry.columns; col++) {
                const cell = document.createElement('div');
                cell.className = 'head-mode-cell';
                cell.dataset.row = String(modelRow);
                cell.dataset.col = String(col);
                if (selected.has(`${modelRow}:${col}`)) cell.classList.add('selected');
                if (modelRow === anchor.row && col === anchor.col) cell.classList.add('anchor');
                if (normalized.subset_type === 'rectangle') {
                    cell.addEventListener('mousedown', () => {
                        rectangleDragActive = true;
                        const counts = rectangleCountsFromCell(geometry, subsetConfig.value, modelRow, col);
                        draftMode = {
                            subset_type: 'rectangle',
                            subset_config: subsetConfig.value,
                            row_count: counts.row_count,
                            column_count: counts.column_count,
                        };
                        renderHeadModeGrid();
                    });
                    cell.addEventListener('mouseenter', () => {
                        if (!rectangleDragActive) return;
                        const counts = rectangleCountsFromCell(geometry, subsetConfig.value, modelRow, col);
                        draftMode = {
                            subset_type: 'rectangle',
                            subset_config: subsetConfig.value,
                            row_count: counts.row_count,
                            column_count: counts.column_count,
                        };
                        renderHeadModeGrid();
                    });
                }
                grid.appendChild(cell);
            }
        }
        summary.textContent =
            `${describeHeadMode(normalized)}. ` +
            `${normalized.num_channels} barrel${normalized.num_channels === 1 ? '' : 's'} active on ` +
            `${headType.replace(/^HT_/, '')}.`;
        subsetType.value = normalized.subset_type;
        subsetConfig.value = normalized.subset_config;
        const procHeadMode = document.getElementById('proc-head-mode');
        if (procHeadMode && !overlay.classList.contains('open')) {
            procHeadMode.textContent = describeHeadMode(state.headMode);
        }
    }

    function openHeadModeModal() {
        draftMode = state.headMode || { subset_type: 'all_barrels', subset_config: 'back_left' };
        renderHeadModeGrid();
        overlay.classList.add('open');
    }

    function closeHeadModeModal() {
        overlay.classList.remove('open');
    }

    async function saveHeadMode() {
        const res = await apiCall('/api/head_mode', 'PUT', {
            subset_type: subsetType.value,
            subset_config: subsetConfig.value,
            row_count: subsetType.value === 'row'
                ? Number(countInput?.value || 1)
                : (subsetType.value === 'rectangle' ? Number(draftMode?.row_count || 1) : undefined),
            column_count: subsetType.value === 'column'
                ? Number(countInput?.value || 1)
                : (subsetType.value === 'rectangle' ? Number(draftMode?.column_count || 1) : undefined),
        });
        if (!res) return;
        state.headType = res.head_type || state.headType;
        state.headMode = res.head_mode || state.headMode;
        await refreshStateNow();
        const procHeadMode = document.getElementById('proc-head-mode');
        if (procHeadMode) procHeadMode.textContent = describeHeadMode(state.headMode);
        log(`Head mode set to ${describeHeadMode(state.headMode)}`, 'success');
        closeHeadModeModal();
    }

    async function suggestHeadMode() {
        const location = parseInt(document.getElementById('proc-location')?.value || '1', 10);
        const res = await apiCall(`/api/head_mode/suggest?location=${location}`, 'GET');
        if (!res?.head_mode) return;
        draftMode = res.head_mode;
        renderHeadModeGrid();
        log(`Suggested head mode for location ${location}: ${describeHeadMode(draftMode)}`, 'info');
    }

    btnOpen?.addEventListener('click', openHeadModeModal);
    btnClose?.addEventListener('click', closeHeadModeModal);
    btnCancel?.addEventListener('click', closeHeadModeModal);
    overlay?.addEventListener('click', e => { if (e.target === overlay) closeHeadModeModal(); });
    document.addEventListener('mouseup', () => { rectangleDragActive = false; });
    subsetType?.addEventListener('change', () => {
        draftMode = {
            subset_type: subsetType.value,
            subset_config: subsetConfig.value,
            row_count: subsetType.value === 'row' ? Number(countInput?.value || 1) : draftMode?.row_count,
            column_count: subsetType.value === 'column' ? Number(countInput?.value || 1) : draftMode?.column_count,
        };
        if (subsetType.value === 'rectangle') {
            draftMode.row_count = Number(draftMode?.row_count || 1);
            draftMode.column_count = Number(draftMode?.column_count || 1);
        }
        renderHeadModeGrid();
    });
    subsetConfig?.addEventListener('change', () => {
        draftMode = {
            subset_type: subsetType.value,
            subset_config: subsetConfig.value,
            row_count: draftMode?.row_count,
            column_count: draftMode?.column_count,
        };
        renderHeadModeGrid();
    });
    countInput?.addEventListener('input', () => {
        draftMode = {
            subset_type: subsetType.value,
            subset_config: subsetConfig.value,
            row_count: subsetType.value === 'row' ? Number(countInput.value || 1) : draftMode?.row_count,
            column_count: subsetType.value === 'column' ? Number(countInput.value || 1) : draftMode?.column_count,
        };
        renderHeadModeGrid();
    });
    btnReset?.addEventListener('click', () => {
        draftMode = { subset_type: 'all_barrels', subset_config: 'back_left' };
        renderHeadModeGrid();
    });
    btnSuggest?.addEventListener('click', suggestHeadMode);
    btnSave?.addEventListener('click', saveHeadMode);
}

// Load profile on startup
setTimeout(loadLabwareCatalog, 500);
setTimeout(loadProfile, 1000);

// ══════════════════════════════════════════════════════════════════════
// LOGGING
// ══════════════════════════════════════════════════════════════════════

function log(message, level = 'info') {
    const logEl = document.getElementById('log');
    const entry = document.createElement('div');
    entry.className = `entry ${level}`;
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${message}`;
    logEl.appendChild(entry);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 200) {
        logEl.removeChild(logEl.firstChild);
    }
}

// ══════════════════════════════════════════════════════════════════════
// RESIZE & RENDER LOOP
// ══════════════════════════════════════════════════════════════════════

function onResize() {
    const rect = viewport.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) {
        return;
    }
    camera.aspect = rect.width / rect.height;
    camera.updateProjectionMatrix();
    renderer.setSize(rect.width, rect.height);
}

window.addEventListener('resize', onResize);
onResize();

function updateURDFJointsFromPositions(positions) {
    if (!urdfRobot) return;
    for (const [jointName, info] of Object.entries(JOINT_AXIS_MAP)) {
        const pos = positions[info.bravoAxis];
        if (pos === undefined) continue;
        const coupledPos = info.coupledAxis ? (positions[info.coupledAxis] ?? 0) : 0;
        const effectivePos = pos + coupledPos * (info.coupledScale ?? 0);
        const value = info.scale * (effectivePos - info.homeOffset) / 1000;
        urdfRobot.setJointValue(jointName, value);
    }
}

let lastAnimationFrameAt = performance.now();

function animate() {
    requestAnimationFrame(animate);
    const now = performance.now();
    const dt = Math.max(0.001, (now - lastAnimationFrameAt) / 1000);
    lastAnimationFrameAt = now;

    // Smooth camera fly-to animation.
    // We bypass controls.update() while animating because OrbitControls with
    // enableDamping re-derives camera.position from its own internal spherical
    // state every frame, which overwrites any external lerpVectors we apply.
    // Instead we set position + lookAt manually, then sync the controls once
    // at the end so user orbit/pan/zoom picks up from the correct position.
    if (camAnim.active) {
        const t = Math.min((performance.now() - camAnim.startMs) / camAnim.durMs, 1);
        // Ease in-out cubic
        const k = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;

        camera.position.lerpVectors(camAnim.fromPos, camAnim.toPos, k);
        controls.target.lerpVectors(camAnim.fromLook, camAnim.toLook, k);
        camera.lookAt(controls.target);  // update quaternion without controls overriding position

        if (t >= 1) {
            camAnim.active = false;
            // Sync OrbitControls' internal spherical state to the final position
            // so the next user interaction starts from here.
            controls.enableDamping = false;
            controls.update();
            controls.enableDamping = true;
        }
    } else {
        controls.update();
    }

    const lerpAlpha = 1 - Math.exp(-MOTION_ANIMATION.smoothingHz * dt);
    for (const axis of Object.keys(state.renderPositions)) {
        const hasMotionTarget = Object.prototype.hasOwnProperty.call(state.motionTargets, axis);
        const target = hasMotionTarget ? state.motionTargets[axis] : state.positions[axis];
        const current = state.renderPositions[axis];
        if (typeof target !== 'number' || typeof current !== 'number') continue;
        const delta = target - current;
        state.renderPositions[axis] =
            Math.abs(delta) <= MOTION_ANIMATION.snapDistanceMm
                ? target
                : current + delta * lerpAlpha;
    }
    updateURDFJointsFromPositions(state.renderPositions);
    updateLabwareAnimation();

    renderer.render(scene, camera);
    gizmoCamera.position.set(0, 0, 3);
    gizmoCamera.position.applyQuaternion(camera.quaternion);
    gizmoCamera.lookAt(0, 0, 0);
    gizmoRenderer.render(gizmoScene, gizmoCamera);
}

animate();

// ── Viewer keyboard shortcuts ─────────────────────────────────────────
// Skip when focus is inside an <input>, <select>, or <textarea>.
const VIEW_KEYS = { f: 'back', d: 'front', r: 'right', e: 'left', c: 'top', v: 'bottom', b: 'iso' };

document.addEventListener('keydown', (ev) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return;
    const preset = VIEW_KEYS[ev.key.toLowerCase()];
    if (preset) {
        ev.preventDefault();
        goToView(preset);
    }
});
ensureEditorLinks();
connectWebSocket();
void loadProfile();
void refreshStateNow();
log('pyBravo', 'success');

// ══════════════════════════════════════════════════════════════════════
// DIAGNOSTICS: Force-limited Z jog with selectable current limit
// ══════════════════════════════════════════════════════════════════════

const diagCheckbox = document.getElementById('diag-enable');
const diagPanel = document.getElementById('diag-panel');
const diagCurrentSelect = document.getElementById('diag-current-limit');

// ST current limits from profile (populated on profile load)
function diagPopulateCurrentLimits() {
    if (!diagCurrentSelect) return;
    // Default ST table for 384 head — will be overridden if profile has current_limits
    const defaults = [
        { tips: 1, current: 0.04 },
        { tips: 8, current: 0.10 },
        { tips: 12, current: 0.10 },
        { tips: 16, current: 0.10 },
        { tips: 24, current: 0.10 },
        { tips: 96, current: 0.30 },
        { tips: 384, current: 0.80 },
    ];
    diagCurrentSelect.innerHTML = '';
    for (const entry of defaults) {
        const opt = document.createElement('option');
        opt.value = entry.current;
        opt.textContent = `${entry.current.toFixed(2)}A (${entry.tips} tips)`;
        diagCurrentSelect.appendChild(opt);
    }
    // Default to 16-tip value (0.10A)
    diagCurrentSelect.value = '0.1';
}

diagCheckbox?.addEventListener('change', () => {
    if (!diagPanel) return;
    diagPanel.style.display = diagCheckbox.checked ? 'block' : 'none';
});

function diagGetPeakCurrent() {
    if (!diagCheckbox?.checked) return undefined;
    const val = parseFloat(diagCurrentSelect?.value);
    return isNaN(val) ? undefined : val;
}

diagPopulateCurrentLimits();

// Remove unused graph update function
function diagUpdateCurrentGraph() {}

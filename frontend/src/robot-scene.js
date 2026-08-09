/**
 * RobotScene — Reusable 3D robot scene for pyBravo.
 *
 * Encapsulates URDF loading, labware rendering, motion interpolation,
 * and camera controls. Can be instantiated independently by multiple pages
 * (main control UI, workflow editor, etc.).
 *
 * Usage:
 *   const scene = new RobotScene(containerElement, { wsUrl: 'ws://...' });
 *   await scene.init();
 *   scene.setDeckDetails(deckDetailsObject);
 *   scene.dispose();
 */

import * as THREE from 'three';
import * as SkeletonUtils from 'three/addons/utils/SkeletonUtils.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

// ── Constants ─────────────────────────────────────────────────────────

const MOTION_ANIMATION = {
    smoothingHz: 12,
    snapDistanceMm: 0.02,
};

const HEAD_CARRIAGE_VISUAL_Y_OFFSET_M = 0.008;
const FINGER_VISUAL_Z_OFFSET_M = 0.012;
const FINGER_JOINT_Y_OFFSET_M = -0.010;
const TOOLING_ASSEMBLY_VISUAL_Y_OFFSET_M = -0.006;
const LABWARE_CARRY_CLEARANCE_M = 0.04;
const DECK_SLOT_SURFACE_OFFSET_M = 0.005;

const DECK_POSITION_GREYS = [
    0.74, 0.70, 0.66, 0.62, 0.58, 0.54, 0.50, 0.46, 0.42,
];

const LABWARE_APPEARANCE_OVERRIDES = {
    '384 Greiner 781091 PS uclear': {
        color: 0xeef2f8, transparent: true, opacity: 0.65,
        roughness: 0.12, metalness: 0.02,
    },
};

const JOINT_AXIS_MAP = {
    'xaxis':          { bravoAxis: 'X',  homeOffset: 193.04, scale:  1 },
    'yaxis':          { bravoAxis: 'Y',  homeOffset: 0,      scale:  1 },
    'zaxis':          { bravoAxis: 'Z',  homeOffset: 0,      scale: -1 },
    'zaxis-gripper':  {
        bravoAxis: 'Zg',
        homeOffset: -20,
        scale: 1,
        coupledAxis: 'Z',
        coupledScale: 1,
    },
    'ygripper-left':  { bravoAxis: 'G',  homeOffset: 0,      scale:  0.5 },
    'ygripper-right': { bravoAxis: 'G',  homeOffset: 0,      scale:  0.5 },
};

const URDF_URL  = '/model/pybravo_urdf/robot.urdf?v=deckfix4';
const ASSET_BASE = '/model/pybravo_urdf/assets';

// ── Helpers ───────────────────────────────────────────────────────────

function parseVec3(str) {
    if (!str) return [0, 0, 0];
    return str.trim().split(/\s+/).map(parseFloat);
}

function isDeckPositionVisual(name) {
    return /^hw1_300_001(?:_\d+)?_hw1_300_001(?:_\d+)?$/i.test(name);
}

function deckLocationFromLinkName(name) {
    const parts = name.replace(/^hw1_300_001_?/i, '').split('_');
    const nums = parts.map(Number).filter(n => !isNaN(n) && n >= 1 && n <= 9);
    return nums.length ? nums[nums.length - 1] : null;
}

function resolveLabwareModelUrl(modelPath) {
    if (!modelPath) return null;
    if (/^(https?:)?\/\//i.test(modelPath) || modelPath.startsWith('/')) {
        return `${modelPath}${modelPath.includes('?') ? '&' : '?'}v=labware3`;
    }
    const encoded = modelPath.split('/').map(part => encodeURIComponent(part)).join('/');
    return `/labware/${encoded}?v=labware3`;
}

function median(values) {
    if (!values.length) return 1;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
}

function fitLinear(samples) {
    if (samples.length < 2) return null;
    const n = samples.length;
    let sumX = 0, sumY = 0, sumXX = 0, sumXY = 0;
    for (const sample of samples) {
        sumX += sample.x; sumY += sample.y;
        sumXX += sample.x * sample.x; sumXY += sample.x * sample.y;
    }
    const denom = n * sumXX - sumX * sumX;
    if (Math.abs(denom) < 1e-9) return null;
    const scale = (n * sumXY - sumX * sumY) / denom;
    const offset = (sumY - scale * sumX) / n;
    return { scale, offset };
}

// ── URDF Model ────────────────────────────────────────────────────────

class URDFModel {
    constructor() {
        this.group = new THREE.Group();
        this._links = {};
        this._joints = {};
    }
    setJointValue(name, valueM) {
        const j = this._joints[name];
        if (!j) return;
        j.child.position.copy(j.originPos).addScaledVector(j.axisInParent, valueM);
    }
}

// ── RobotScene Class ──────────────────────────────────────────────────

export class RobotScene {
    /**
     * @param {HTMLElement} container - DOM element to attach the renderer to
     * @param {Object} options
     * @param {string} [options.wsUrl] - WebSocket URL for state streaming
     * @param {boolean} [options.autoConnect=true] - auto-connect WebSocket
     * @param {boolean} [options.showGizmo=true] - show coordinate gizmo
     * @param {Function} [options.onStateUpdate] - callback with raw state data
     */
    constructor(container, options = {}) {
        this.container = container;
        this.options = {
            wsUrl: `ws://${location.hostname}:8000/ws/state`,
            autoConnect: true,
            showGizmo: true,
            onStateUpdate: null,
            ...options,
        };

        // State
        this.positions = { X: 0, Y: 0, Z: 0, W: 0, G: 0, Zg: 0 };
        this.renderPositions = { X: 0, Y: 0, Z: 0, W: 0, G: 0, Zg: 0 };
        this.motionTargets = {};
        this.deckDetails = {};
        this.teachpoints = {};
        this.deckMotionMap = null;
        this.taskStatus = {};
        this.headType = null;
        this.headMode = null;
        this.tipSelection = null;
        this.tipsOnHead = false;
        this.tipsOnHeadMode = null;
        this.tipsOnHeadSelection = null;
        this.tipLabwareName = '';
        this.tipDefinitionId = '';
        this.attachedTipLengthMm = null;
        this.activeTipCapacityUl = null;
        this.tipboxTransientState = {};
        this._headTipStateSignature = '';
        this._tipboxTransientSignature = '';
        this.simulationMode = false;
        this._wsPaused = false;
        this._debugSimulation = false;
        this._debugThrottle = 0;

        // Three.js
        this.renderer = null;
        this.scene = null;
        this.camera = null;
        this.controls = null;
        this.stlLoader = new STLLoader();
        this.gltfLoader = new GLTFLoader();

        // URDF
        this.urdfRobot = null;
        this.labwareRoot = new THREE.Group();
        this.labwareRoot.name = 'labware-root';
        this.headTipsRoot = new THREE.Group();
        this.headTipsRoot.name = 'head-tips-root';
        this.deckSlotAnchors = new Map();
        this.deckSlotLinkNames = new Map();
        this.deckLabwareMeshes = new Map();
        this.labwareTemplateCache = new Map();
        this.tipTemplateCache = new Map();
        this.labwareRefreshToken = 0;

        // Carry animation
        this.carryAnimation = { active: false, sourceLoc: null, offset: null };

        // Camera animation
        this.modelCenter = new THREE.Vector3();
        this.modelSize = new THREE.Vector3();
        this.isoPosition = new THREE.Vector3(0.6, 0.5, 0.6);
        this.camAnim = {
            active: false, startMs: 0, durMs: 400,
            fromPos: new THREE.Vector3(), toPos: new THREE.Vector3(),
            fromLook: new THREE.Vector3(), toLook: new THREE.Vector3(),
        };

        // Gizmo
        this.gizmoRenderer = null;
        this.gizmoScene = null;
        this.gizmoCamera = null;

        // WebSocket
        this.ws = null;
        this._wsReconnectTimer = null;

        // Animation
        this._animationId = null;
        this._lastAnimationFrameAt = performance.now();
        this._disposed = false;
    }

    // ── Lifecycle ─────────────────────────────────────────────────────

    async init() {
        this._setupRenderer();
        this._setupLighting();
        if (this.options.showGizmo) this._setupGizmo();
        await this._loadURDF();
        this._startAnimation();
        if (this.options.autoConnect) this.connectWebSocket();
        return this;
    }

    dispose() {
        this._disposed = true;
        if (this._animationId) cancelAnimationFrame(this._animationId);
        if (this._wsReconnectTimer) clearTimeout(this._wsReconnectTimer);
        if (this.ws) { this.ws.onclose = null; this.ws.close(); }
        if (this._resizeObserver) this._resizeObserver.disconnect();
        this.controls?.dispose();
        this.renderer?.dispose();
        if (this.gizmoRenderer) this.gizmoRenderer.dispose();
        while (this.container.firstChild) {
            this.container.removeChild(this.container.firstChild);
        }
    }

    // ── Public API ────────────────────────────────────────────────────

    /** Update deck labware from external source (workflow editor deck config). */
    setDeckDetails(deckDetails) {
        this.deckDetails = deckDetails;
        void this.refreshDeckLabwareScene();
    }

    /** Set axis positions (the "truth" each axis is driving toward).
     *
     * Intentionally does NOT snap ``renderPositions`` — the animate
     * loop's lerp handles visible motion so the URDF interpolates
     * smoothly between successive setPositions calls. This matters
     * most for live workflow execution, where each ``workflow:positions``
     * event reports the END of a step (often a big X / Y / Z jump from
     * the previous step). Snapping here made the URDF teleport
     * between those keyframes, which looked like the robot "bouncing
     * back and forth" for fast sequences like retract → move → descend.
     *
     * Callers that genuinely need an immediate visual snap (e.g. the
     * scrubber so slider-drag is responsive) should also call
     * :meth:`snapRenderPositions` after :meth:`setPositions`.
     */
    setPositions(positions) {
        if (this._debugSimulation) {
            console.log('[RobotScene] setPositions:', JSON.stringify(positions));
        }
        for (const [axis, value] of Object.entries(positions)) {
            this.positions[axis] = value;
        }
        // Diagnostic: print head-bottom and tip-bottom world Z so operators
        // can compare against catalog teach_z in the backend logs. Gated on
        // `window.__pybravoDebugTipZ` to stay silent unless explicitly enabled.
        if (typeof window !== 'undefined' && window.__pybravoDebugTipZ && positions && positions.Z !== undefined) {
            const headBottomZ = Number(positions.Z) || 0;
            const tipLengthMm = Number(this.attachedTipLengthMm) || 0;
            const tipBottomZ = this.tipsOnHead ? headBottomZ + tipLengthMm : null;
            console.log(
                '[RobotScene] Z diag: head_bottom_Z_mm=%s tip_bottom_Z_mm=%s (tipsOnHead=%s, attached_tip_length_mm=%s)',
                headBottomZ.toFixed(3),
                tipBottomZ === null ? '-' : tipBottomZ.toFixed(3),
                this.tipsOnHead,
                tipLengthMm,
            );
        }
    }

    /** Force the URDF to instantly reflect these positions — skips
     * the animate-loop lerp. Call alongside :meth:`setPositions` when
     * instant visual response matters (scrubber drag, seek-to-time).
     * Never call during live workflow execution: that's exactly the
     * case the interpolator is there to smooth over.
     */
    snapRenderPositions(positions) {
        for (const [axis, value] of Object.entries(positions)) {
            this.renderPositions[axis] = value;
        }
    }

    /** Set motion targets for smooth interpolation. */
    setMotionTargets(targets) {
        this.motionTargets = targets;
    }

    /** Set task status (for carry animation + motion targets).
     *
     * Mirrors the /ws/state merge semantics — axes mentioned in the
     * new status's targets update; axes previously in motionTargets
     * but absent from this status persist. An empty-targets call
     * (setTaskStatus({})) is the escape hatch that fully clears
     * motionTargets for task-end cleanup.
     */
    setTaskStatus(status) {
        this.taskStatus = status;
        const incoming = status.targets || {};
        if (Object.keys(incoming).length > 0) {
            this.motionTargets = { ...this.motionTargets, ...incoming };
        } else {
            this.motionTargets = {};
        }
    }

    getHeadTipState() {
        return JSON.parse(JSON.stringify({
            headType: this.headType,
            headMode: this.headMode,
            tipSelection: this.tipSelection,
            tipsOnHead: this.tipsOnHead,
            tipsOnHeadMode: this.tipsOnHeadMode,
            tipsOnHeadSelection: this.tipsOnHeadSelection,
            tipLabwareName: this.tipLabwareName,
            tipDefinitionId: this.tipDefinitionId,
            attachedTipLengthMm: this.attachedTipLengthMm,
            activeTipCapacityUl: this.activeTipCapacityUl,
        }));
    }

    getTipboxTransientState() {
        return JSON.parse(JSON.stringify(this.tipboxTransientState || {}));
    }

    setTipboxTransientState(state) {
        const nextState = {};
        for (const [location, cells] of Object.entries(state || {})) {
            // Keep empty arrays. "No cells hidden" is real information — it is
            // how a box that started empty and has been filled back up says so
            // — and dropping it made the renderer fall back to the configured
            // fill state and draw nothing.
            if (!Array.isArray(cells)) continue;
            nextState[String(location)] = [...new Set(cells.map(String))].sort();
        }
        const signature = JSON.stringify(nextState);
        if (signature === this._tipboxTransientSignature) return;
        this._tipboxTransientSignature = signature;
        this.tipboxTransientState = nextState;
        void this.refreshDeckLabwareScene();
    }

    async setHeadTipState(state = {}) {
        if (state.headType) this.headType = state.headType;
        if (state.head_type) this.headType = state.head_type;
        if (state.headMode !== undefined) this.headMode = state.headMode;
        if (state.head_mode !== undefined) this.headMode = state.head_mode;
        if (state.tipSelection !== undefined) this.tipSelection = state.tipSelection;
        if (state.tip_selection !== undefined) this.tipSelection = state.tip_selection;
        if (state.tipsOnHead !== undefined) this.tipsOnHead = !!state.tipsOnHead;
        if (state.tips_on_head !== undefined) this.tipsOnHead = !!state.tips_on_head;
        if (state.tipsOnHeadMode !== undefined) this.tipsOnHeadMode = state.tipsOnHeadMode;
        if (state.tips_on_head_mode !== undefined) this.tipsOnHeadMode = state.tips_on_head_mode;
        if (state.tipsOnHeadSelection !== undefined) this.tipsOnHeadSelection = state.tipsOnHeadSelection;
        if (state.tips_on_head_selection !== undefined) this.tipsOnHeadSelection = state.tips_on_head_selection;
        if (state.tipLabwareName !== undefined) this.tipLabwareName = state.tipLabwareName || '';
        if (state.tip_labware_name !== undefined) this.tipLabwareName = state.tip_labware_name || '';
        if (state.tipDefinitionId !== undefined) this.tipDefinitionId = state.tipDefinitionId || '';
        if (state.tip_definition_id !== undefined) this.tipDefinitionId = state.tip_definition_id || '';
        if (state.attachedTipLengthMm !== undefined) this.attachedTipLengthMm = state.attachedTipLengthMm;
        if (state.attached_tip_length_mm !== undefined) this.attachedTipLengthMm = state.attached_tip_length_mm;
        if (state.activeTipCapacityUl !== undefined) this.activeTipCapacityUl = state.activeTipCapacityUl;
        if (state.active_tip_capacity_ul !== undefined) this.activeTipCapacityUl = state.active_tip_capacity_ul;
        if (state.tipboxTransientState !== undefined) this.setTipboxTransientState(state.tipboxTransientState);
        if (state.tipbox_removed_cells !== undefined) this.setTipboxTransientState(state.tipbox_removed_cells);

        const signature = JSON.stringify(this.getHeadTipState());
        if (signature === this._headTipStateSignature) return;
        this._headTipStateSignature = signature;
        await this._renderHeadTipsFromState();
    }

    /**
     * Enable/disable simulation mode.
     * While active, position and task_status updates from the /ws/state
     * WebSocket are ignored so they don't overwrite executor animation positions.
     */
    setSimulationMode(enabled) {
        this.simulationMode = !!enabled;
        this._debugSimulation = !!enabled;
        if (enabled) {
            console.log('[RobotScene] Simulation mode ENABLED — pausing /ws/state WebSocket');
            // Completely disconnect the hardware state WebSocket during simulation
            // to guarantee no position overwrites (belt-and-suspenders with the
            // simulationMode flag guard in _handleStateUpdate).
            this.pauseWebSocket();
        } else {
            console.log('[RobotScene] Simulation mode DISABLED — resuming /ws/state WebSocket');
            this.resumeWebSocket();
        }
    }

    /** Set teachpoints for deck motion mapping. */
    setTeachpoints(teachpoints) {
        this.teachpoints = teachpoints;
        this._updateDeckMotionMapping();
    }

    /** Fly camera to a preset view. */
    goToView(preset) {
        if (!this.modelCenter || this.modelSize.length() === 0) return;
        const c = this.modelCenter;
        const s = this.modelSize;
        const d = Math.max(s.x, s.y, s.z) * 1.5;
        const views = {
            front:  { pos: [c.x, c.y, c.z + d],  look: c },
            back:   { pos: [c.x, c.y, c.z - d],  look: c },
            left:   { pos: [c.x - d, c.y, c.z],   look: c },
            right:  { pos: [c.x + d, c.y, c.z],   look: c },
            top:    { pos: [c.x, c.y + d, c.z],   look: c },
            bottom: { pos: [c.x, c.y - d, c.z],   look: c },
            iso:    { pos: this.isoPosition.clone(), look: c },
        };
        const view = views[preset];
        if (!view) return;
        const target = view.look instanceof THREE.Vector3 ? view.look : new THREE.Vector3(...view.look);
        const pos = view.pos instanceof THREE.Vector3 ? view.pos : new THREE.Vector3(...view.pos);
        this.camAnim.fromPos.copy(this.camera.position);
        this.camAnim.toPos.copy(pos);
        this.camAnim.fromLook.copy(this.controls.target);
        this.camAnim.toLook.copy(target);
        this.camAnim.startMs = performance.now();
        this.camAnim.active = true;
    }

    /** Resize renderer to fit container. */
    resize() {
        const rect = this.container.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return;
        this.camera.aspect = rect.width / rect.height;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(rect.width, rect.height);
    }

    /** Pause the /ws/state WebSocket (stop receiving hardware positions). */
    pauseWebSocket() {
        if (this._wsReconnectTimer) {
            clearTimeout(this._wsReconnectTimer);
            this._wsReconnectTimer = null;
        }
        if (this.ws) {
            this._wsPaused = true;
            this.ws.close();
            this.ws = null;
        }
    }

    /** Resume the /ws/state WebSocket connection. */
    resumeWebSocket() {
        this._wsPaused = false;
        if (!this.ws) {
            this.connectWebSocket();
        }
    }

    // ── WebSocket ─────────────────────────────────────────────────────

    connectWebSocket() {
        if (this._disposed || this._wsPaused) return;
        this.ws = new WebSocket(this.options.wsUrl);
        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this._handleStateUpdate(data);
            } catch (e) { /* ignore */ }
        };
        this.ws.onclose = () => {
            if (this._disposed) return;
            this._wsReconnectTimer = setTimeout(() => this.connectWebSocket(), 3000);
        };
        this.ws.onerror = () => {};
    }

    _handleStateUpdate(data) {
        // In simulation mode, ignore hardware positions and task_status from
        // the /ws/state WebSocket so they don't overwrite the executor's
        // pre-computed animation positions.
        if (!this.simulationMode) {
            if (data.positions) {
                for (const [axis, value] of Object.entries(data.positions)) {
                    this.positions[axis] = value;
                }
            }
            if (data.task_status) {
                this.taskStatus = data.task_status;
                // Sticky-merge targets instead of wholesale replace:
                // a single task step's _log_step only lists the axes
                // being commanded *right now* (e.g. move_xy_to_pick
                // sets {X, Y}; the next step move_to_pick_height sets
                // {Z, Zg}). Wholesale-replacing on every /ws/state
                // dropped the prior step's axes out of motionTargets,
                // and the lerp fell back to this.positions[axis] —
                // which lags physical motion by up to 200 ms because
                // /ws/state broadcasts at 5 Hz. The visible result was
                // the URDF racing to a target, then snapping backward
                // to the lagging telemetry sample, then racing forward
                // again on the next frame ("bouncing").
                //
                // Merging preserves the last-known target per axis
                // until the next step explicitly updates it. When a
                // task ends and setTaskStatus({}) is called, the
                // empty-targets branch below fully clears the dict so
                // nothing carries over into the next task.
                const incoming = this.taskStatus.targets || {};
                if (Object.keys(incoming).length > 0) {
                    this.motionTargets = { ...this.motionTargets, ...incoming };
                } else {
                    this.motionTargets = {};
                }
            }
        } else if (this._debugSimulation && data.positions) {
            console.log('[RobotScene] simulationMode ON — ignoring /ws/state positions:',
                `Z=${data.positions.Z}, Zg=${data.positions.Zg}`,
                '| keeping sim positions:',
                `Z=${this.positions.Z}, Zg=${this.positions.Zg}`);
        }
        // Deck details, teachpoints, and runtime head/tip state are always
        // accepted even while simulation playback owns the axis positions.
        if (data.deck_details) {
            const signature = JSON.stringify(data.deck_details);
            if (signature !== this._deckDetailsSignature) {
                this._deckDetailsSignature = signature;
                this.deckDetails = data.deck_details;
                void this.refreshDeckLabwareScene();
            }
        }
        if (data.teachpoints) {
            this.teachpoints = data.teachpoints;
            this._updateDeckMotionMapping();
        }
        if (
            data.head_type !== undefined
            || data.head_mode !== undefined
            || data.tip_selection !== undefined
            || data.tips_on_head !== undefined
            || data.tips_on_head_mode !== undefined
            || data.tips_on_head_selection !== undefined
            || data.tip_labware !== undefined
            || data.tip_labware_name !== undefined
            || data.tip_definition_id !== undefined
            || data.attached_tip_length_mm !== undefined
            || data.active_tip_capacity_ul !== undefined
            || data.tipbox_removed_cells !== undefined
        ) {
            void this.setHeadTipState({
                head_type: data.head_type,
                head_mode: data.head_mode,
                tip_selection: data.tip_selection,
                tips_on_head: data.tips_on_head,
                tips_on_head_mode: data.tips_on_head_mode,
                tips_on_head_selection: data.tips_on_head_selection,
                tip_labware_name: data.tip_labware ?? data.tip_labware_name,
                tip_definition_id: data.tip_definition_id,
                attached_tip_length_mm: data.attached_tip_length_mm,
                active_tip_capacity_ul: data.active_tip_capacity_ul,
                tipbox_removed_cells: data.tipbox_removed_cells,
            });
        }
        if (this.options.onStateUpdate) this.options.onStateUpdate(data);
    }

    // ── Renderer Setup ────────────────────────────────────────────────

    _setupRenderer() {
        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.renderer.setPixelRatio(window.devicePixelRatio);
        this.renderer.outputColorSpace = THREE.SRGBColorSpace;
        this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
        this.renderer.toneMappingExposure = 1.2;
        this.container.appendChild(this.renderer.domElement);

        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color(0x0d0d14);

        this.camera = new THREE.PerspectiveCamera(45, 1, 0.001, 100);
        this.camera.position.set(0.6, 0.5, 0.6);

        this.controls = new OrbitControls(this.camera, this.renderer.domElement);
        this.controls.target.set(0, 0.3, 0);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.08;
        this.controls.update();

        this.resize();
        this._resizeObserver = new ResizeObserver(() => this.resize());
        this._resizeObserver.observe(this.container);
    }

    _setupLighting() {
        const ambient = new THREE.AmbientLight(0xffffff, 1.25);
        this.scene.add(ambient);
        const dir = new THREE.DirectionalLight(0xffffff, 0.55);
        dir.position.set(1, 2, 1.5);
        this.scene.add(dir);
        const fill = new THREE.DirectionalLight(0xffffff, 0.35);
        fill.position.set(-1, 0.5, -1);
        this.scene.add(fill);
    }

    _setupGizmo() {
        const size = 140;
        this.gizmoRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.gizmoRenderer.setPixelRatio(window.devicePixelRatio);
        this.gizmoRenderer.setSize(size, size);
        this.gizmoRenderer.setClearColor(0x000000, 0);
        Object.assign(this.gizmoRenderer.domElement.style, {
            position: 'absolute', bottom: '12px', left: '12px',
            pointerEvents: 'none', borderRadius: '8px',
            background: 'rgba(18, 18, 26, 0.7)',
            border: '1px solid #2a2a3a',
        });
        this.container.appendChild(this.gizmoRenderer.domElement);

        this.gizmoScene = new THREE.Scene();
        this.gizmoCamera = new THREE.PerspectiveCamera(50, 1, 0.1, 10);
        this.gizmoCamera.position.set(0, 0, 3);

        const gizmoGroup = new THREE.Group();
        gizmoGroup.add(this._makeArrow(new THREE.Vector3(1, 0, 0), 0xff4444, '+X', 'Right'));
        gizmoGroup.add(this._makeArrow(new THREE.Vector3(0, 0, 1), 0x44cc44, '+Y', 'Forward'));
        gizmoGroup.add(this._makeArrow(new THREE.Vector3(0, -1, 0), 0x4488ff, '+Z', 'Down'));
        const originGeo = new THREE.SphereGeometry(0.06, 12, 12);
        const originMat = new THREE.MeshBasicMaterial({ color: 0x888888 });
        gizmoGroup.add(new THREE.Mesh(originGeo, originMat));
        this.gizmoScene.add(gizmoGroup);
    }

    _makeArrow(dir, color, label, sublabel) {
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
        canvas.width = 128; canvas.height = 64;
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

    // ── URDF Loading ──────────────────────────────────────────────────

    async _loadURDF() {
        let xmlText;
        try {
            const resp = await fetch(URDF_URL);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            xmlText = await resp.text();
        } catch (e) {
            console.error(`Failed to fetch URDF: ${e.message}`);
            return;
        }

        const xml = new DOMParser().parseFromString(xmlText, 'text/xml');
        const model = new URDFModel();
        this.deckSlotAnchors.clear();

        const linkGroups = {};
        for (const el of xml.querySelectorAll('link')) {
            const name = el.getAttribute('name');
            if (name && !linkGroups[name]) {
                const g = new THREE.Group();
                g.name = name;
                linkGroups[name] = g;
            }
        }

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
                    this.stlLoader.load(filename, (geometry) => {
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
        this._assignDeckPositionColors(deckVisuals);

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
            childGroup.quaternion.setFromEuler(new THREE.Euler(rpy[0], rpy[1], rpy[2], 'ZYX'));
            parentGroup.add(childGroup);
            childLinks.add(childName);

            if (jtype === 'prismatic') {
                const axisEl = joint.querySelector('axis');
                const axisLocal = parseVec3(axisEl?.getAttribute('xyz') ?? '0 0 1');
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

        for (const [name, group] of Object.entries(linkGroups)) {
            if (!childLinks.has(name)) {
                model.group.add(group);
            }
        }
        model._links = linkGroups;
        // Ensure world matrices are computed for the full link hierarchy
        // before measuring deck slot bounding boxes.
        model.group.updateMatrixWorld(true);
        this._buildDeckSlotAnchors(linkGroups);

        model.group.rotation.x = -Math.PI / 2;
        this.labwareRoot.removeFromParent();
        this.labwareRoot.clear();
        model.group.add(this.labwareRoot);
        this.headTipsRoot.removeFromParent();
        this.headTipsRoot.clear();

        this.scene.add(model.group);
        this.urdfRobot = model;

        const box = new THREE.Box3().setFromObject(model.group);
        this.modelCenter = box.getCenter(new THREE.Vector3());
        this.modelSize = box.getSize(new THREE.Vector3());
        this.controls.target.copy(this.modelCenter);
        const isoPos = new THREE.Vector3(
            this.modelCenter.x + this.modelSize.x * 1.2,
            this.modelCenter.y + this.modelSize.y * 0.8,
            this.modelCenter.z + this.modelSize.z * 1.2,
        );
        this.camera.position.copy(isoPos);
        this.isoPosition.copy(isoPos);
        this.controls.update();
        void this.refreshDeckLabwareScene();
    }

    _assignDeckPositionColors(deckVisuals) {
        const sorted = [...deckVisuals].sort((a, b) => {
            const dy = a.y - b.y;
            return Math.abs(dy) > 0.01 ? -dy : a.x - b.x;
        });
        sorted.forEach((entry, index) => {
            const grey = DECK_POSITION_GREYS[index] ?? 0.6;
            entry.mesh.material.color.setRGB(grey, grey, grey + 0.03);
            const loc = deckLocationFromLinkName(entry.linkName);
            if (loc) this.deckSlotLinkNames.set(loc, entry.linkName);
        });
    }

    _buildDeckSlotAnchors(linkGroups) {
        this.deckSlotAnchors.clear();
        for (const [loc, linkName] of this.deckSlotLinkNames.entries()) {
            const group = linkGroups[linkName];
            if (!group) continue;
            const box = new THREE.Box3().setFromObject(group);
            const center = box.getCenter(new THREE.Vector3());
            const anchor = new THREE.Vector3(center.x, center.y, box.max.z - DECK_SLOT_SURFACE_OFFSET_M);
            this.deckSlotAnchors.set(loc, anchor);
            console.log(`[DeckAnchor] slot ${loc}: anchor=(${(anchor.x*1000).toFixed(1)}, ${(anchor.y*1000).toFixed(1)}, ${(anchor.z*1000).toFixed(1)})mm, box.max.z=${(box.max.z*1000).toFixed(1)}mm`);
        }
        this._updateDeckMotionMapping();
    }

    _updateDeckMotionMapping() {
        const xSamples = [];
        const ySamples = [];
        for (let loc = 1; loc <= 9; loc++) {
            const anchor = this.deckSlotAnchors.get(loc);
            const tp = this.teachpoints?.[String(loc)] || this.teachpoints?.[loc];
            if (!anchor || !tp) continue;
            if (typeof tp.x === 'number') xSamples.push({ x: tp.x, y: anchor.x });
            if (typeof tp.y === 'number') ySamples.push({ x: tp.y, y: anchor.y });
        }
        const xFit = fitLinear(xSamples);
        const yFit = fitLinear(ySamples);
        this.deckMotionMap = xFit && yFit ? { x: xFit, y: yFit } : null;
    }

    _detailWellDimensions(detail) {
        const wellDimensions = detail?.well_dimensions_mm;
        return wellDimensions && typeof wellDimensions === 'object' ? wellDimensions : (detail || {});
    }

    _labwareCenterOffsetFromTeachpointMm(detail) {
        const wellDimensions = this._detailWellDimensions(detail);
        const lengthMm = Math.max(0.0, Number(detail?.length_mm || detail?.length || 0.0));
        const widthMm = Math.max(0.0, Number(detail?.width_mm || detail?.width || 0.0));
        const offsetXmm = Number(wellDimensions?.offset_x_mm ?? detail?.offset_x_mm ?? 0.0);
        const offsetYmm = Number(wellDimensions?.offset_y_mm ?? detail?.offset_y_mm ?? 0.0);
        return {
            x: (lengthMm / 2.0) - offsetXmm,
            y: (widthMm / 2.0) - offsetYmm,
        };
    }

    // ── Labware Rendering ─────────────────────────────────────────────

    async refreshDeckLabwareScene() {
        const token = ++this.labwareRefreshToken;
        if (!this.urdfRobot || this.deckSlotAnchors.size === 0) return;

        const nextEntries = [];
        for (let loc = 1; loc <= 9; loc++) {
            const items = this.deckDetails[String(loc)];
            const details = Array.isArray(items) ? items : [];
            const anchor = this.deckSlotAnchors.get(loc);
            if (!details.length || !anchor) continue;

            let supportHeightM = 0;
            for (let index = 0; index < details.length; index++) {
                const detail = details[index];
                const mesh = await this._buildLabwareMesh(detail);
                if (!mesh) continue;
                const entryAnchor = anchor.clone();
                entryAnchor.z += supportHeightM;
                mesh.position.copy(entryAnchor);
                const entry = { group: mesh, anchor: entryAnchor.clone(), detail, location: loc };
                await this._attachTipboxTips(entry, detail);
                nextEntries.push([loc, entry, index === details.length - 1]);
                supportHeightM += Math.max(
                    0.001,
                    Number(detail?.stack_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000,
                );
            }
        }

        if (token !== this.labwareRefreshToken) return;
        this._resetCarryAnimation();
        this.deckLabwareMeshes.clear();
        this.labwareRoot.clear();
        for (const [loc, entry, isTop] of nextEntries) {
            if (isTop) this.deckLabwareMeshes.set(loc, entry);
            this.labwareRoot.add(entry.group);
        }
    }

    async _buildLabwareMesh(detail) {
        if (String(detail?.render_mode || '').toLowerCase() === 'generated_lid'
            || String(detail?.base_class || '').toLowerCase() === 'lid'
            || String(detail?.kind || '').toLowerCase() === 'lid') {
            return this._buildGeneratedLidMesh(detail);
        }
        const baseDetail = detail?.generated_lid
            ? { ...detail, height_mm: Number(detail?.base_height_mm || detail?.height_mm || detail?.height || 14.4) }
            : detail;
        const url = resolveLabwareModelUrl(detail?.model_3d);
        if (!url) return this._attachGeneratedLidMesh(this._buildFallbackLabwareMesh(baseDetail), detail);

        const cacheKey = `${url}|${baseDetail.length_mm || 0}|${baseDetail.width_mm || 0}|${baseDetail.height_mm || baseDetail.height || 0}`;
        if (!this.labwareTemplateCache.has(cacheKey)) {
            this.labwareTemplateCache.set(cacheKey, new Promise((resolve) => {
                this.gltfLoader.load(url, (gltf) => {
                    const wrapper = new THREE.Group();
                    const source = gltf.scene || gltf.scenes?.[0];
                    if (!source) {
                        resolve(this._buildFallbackLabwareMesh(baseDetail));
                        return;
                    }
                    const clone = SkeletonUtils.clone(source);
                    wrapper.add(clone);
                    this._normalizeLabwareModel(clone, baseDetail);
                    resolve(wrapper);
                }, undefined, () => resolve(this._buildFallbackLabwareMesh(baseDetail)));
            }));
        }
        const template = await this.labwareTemplateCache.get(cacheKey);
        return template ? this._attachGeneratedLidMesh(template.clone(true), detail) : null;
    }

    _normalizeLabwareModel(model, detail) {
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
                sourceSize.slice().sort((a, b) => a - b)
                    .map((value, index) => targetSize.slice().sort((a, b) => a - b)[index] / value),
            );
            if (Number.isFinite(scale) && scale > 0) model.scale.setScalar(scale);
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

    _buildFallbackLabwareMesh(detail) {
        const lengthM = Math.max(0.01, Number(detail?.length_mm || detail?.length || 127.76) / 1000);
        const widthM = Math.max(0.01, Number(detail?.width_mm || detail?.width || 85.48) / 1000);
        const heightM = Math.max(0.006, Number(detail?.height_mm || detail?.height || detail?.stack_height_mm || 14.4) / 1000);
        const color = (() => {
            const baseClass = String(detail?.base_class || '').toLowerCase();
            const kind = String(detail?.kind || '').toLowerCase();
            if (baseClass === 'tip_box' || kind === 'tip_box') return 0xcfd3da;
            if (baseClass === 'tip_trash' || kind === 'tip_trash') return 0xa6adb8;
            return 0xe4e6eb;
        })();
        const geometry = new THREE.BoxGeometry(lengthM, widthM, heightM);
        const material = new THREE.MeshStandardMaterial({ color, roughness: 0.82, metalness: 0.03 });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        mesh.position.z = heightM / 2;
        const wrapper = new THREE.Group();
        wrapper.add(mesh);
        return wrapper;
    }

    _buildGeneratedLidMesh(detail, options = {}) {
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
            roughness: 0.28, metalness: 0.02,
        });
        const top = new THREE.Mesh(new THREE.BoxGeometry(lengthM, widthM, topThicknessM), material.clone());
        top.position.z = heightM - (topThicknessM / 2);
        top.castShadow = true; top.receiveShadow = true;
        group.add(top);
        const wallGeoX = new THREE.BoxGeometry(lengthM, skirtThicknessM, skirtHeightM);
        const wallGeoY = new THREE.BoxGeometry(skirtThicknessM, widthM, skirtHeightM);
        const hL = lengthM / 2, hW = widthM / 2, wZ = skirtHeightM / 2;
        const walls = [
            [wallGeoX, 0, hW - skirtThicknessM / 2, wZ],
            [wallGeoX, 0, -hW + skirtThicknessM / 2, wZ],
            [wallGeoY, hL - skirtThicknessM / 2, 0, wZ],
            [wallGeoY, -hL + skirtThicknessM / 2, 0, wZ],
        ];
        walls.forEach(([geo, x, y, z]) => {
            const wall = new THREE.Mesh(geo, material.clone());
            wall.position.set(x, y, z);
            wall.castShadow = true; wall.receiveShadow = true;
            group.add(wall);
        });
        return group;
    }

    _attachGeneratedLidMesh(wrapper, detail) {
        const lidDetail = detail?.generated_lid;
        if (!wrapper || !lidDetail) return wrapper;
        const lid = this._buildGeneratedLidMesh(lidDetail, { attached: true });
        const baseHeightM = Math.max(0.001, Number(detail?.base_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000);
        const totalHeightM = Math.max(baseHeightM, Number(detail?.total_height_mm || detail?.height_mm || detail?.height || 14.4) / 1000);
        const lidHeightM = Math.max(0.001, Number(lidDetail?.height_mm || lidDetail?.lid_thickness_mm || 1.0) / 1000);
        lid.position.z = Math.max(0, totalHeightM - lidHeightM);
        wrapper.add(lid);
        return wrapper;
    }

    // ── Tip Rendering ─────────────────────────────────────────────────

    static HEAD_GEOMETRY = {
        HT_384_D_70:      { rows: 16, columns: 24 },
        HT_384_D_70_S2:   { rows: 16, columns: 24 },
        HT_384_F_50:      { rows: 16, columns: 24 },
        HT_384_PINTOOL:   { rows: 16, columns: 24 },
        HT_1536_PINTOOL:  { rows: 32, columns: 48 },
        HT_16_D_ST:       { rows: 16, columns: 1 },
        HT_8_D_LT:        { rows: 8,  columns: 1 },
    };

    _getHeadGeometry(headType) {
        return RobotScene.HEAD_GEOMETRY[headType] || { rows: 8, columns: 12 };
    }

    _normalizeHeadMode(headType, headMode = null) {
        const geometry = this._getHeadGeometry(headType);
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
            rowCount = Math.max(1, Math.min(geometry.rows, Number(headMode?.row_count || geometry.rows)));
            columnCount = Math.max(1, Math.min(geometry.columns, Number(headMode?.column_count || geometry.columns)));
        } else if (subsetType === 'single_barrel') {
            rowCount = 1;
            columnCount = 1;
        }
        return {
            subset_type: subsetType,
            subset_config: subsetConfig,
            row_count: rowCount,
            column_count: columnCount,
        };
    }

    _selectedHeadCells(headType, headMode = null) {
        const geometry = this._getHeadGeometry(headType);
        const normalized = this._normalizeHeadMode(headType, headMode);
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
        return { geometry, selected };
    }

    _getTipboxGeometry(detail) {
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

    _tipLengthMmForDetail(detail, capacityUl = null) {
        const explicitLengthMm = Number(
            detail?.tip_length_mm
            || detail?.disposable_tip_length_mm
            || detail?.attached_tip_length_mm
            || 0,
        );
        if (explicitLengthMm > 0) return explicitLengthMm;
        const capacity = Number(capacityUl ?? detail?.disposable_tip_capacity_ul ?? this.activeTipCapacityUl ?? 0);
        if (capacity > 0 && capacity <= 10) return 19.9;
        if (capacity > 10 && capacity <= 70) return 26.1;
        if (capacity >= 200) return 50.0;
        return 26.1;
    }

    _getHeadTipPitchMm(headType) {
        const geometry = this._getHeadGeometry(headType);
        if (geometry.columns >= 48 || geometry.rows >= 32) return { pitchX: 2.25, pitchY: 2.25 };
        if (geometry.columns >= 24 || geometry.rows >= 16) return { pitchX: 4.5, pitchY: 4.5 };
        return { pitchX: 9.0, pitchY: 9.0 };
    }

    _getHeadTipHostLink() {
        if (!this.urdfRobot?._links) return null;
        return (
            this.urdfRobot._links['384_head_384_head']
            || this.urdfRobot._links['384_head']
            || this.urdfRobot._links.zaxis
            || this.urdfRobot._links.head
            || this.urdfRobot._links.tool
            || this.urdfRobot._links.gripperzaxis
            || this.urdfRobot.group
        );
    }

    /**
     * Where the barrel array sits in the head link, derived from the deck.
     *
     * `_getHeadTipMountFrame` infers the array from the bounding box of the
     * head mesh's bottom slice, which picks up shroud and mounting structure
     * and leaves the rendered barrels a constant ~10.4 mm (2.3 columns) off.
     * That is invisible mid-array in a full box and obvious at the edge of an
     * empty one.
     *
     * The physical invariant is that the head's A1 barrel sits exactly over the
     * tip box's A1 well. Both are 16x24 grids on the same pitch, so the two
     * arrays coincide, and the head's array centre can be derived rather than
     * measured off a mesh:
     *
     *   - deck slot anchors are slot-mesh centres, and a box is drawn centred
     *     on its slot, so mapping robot XY lands on the slot centre;
     *   - commanded XY is teachpoint + (col * pitch - offset), so for the A1
     *     anchor it sits one A1 offset short of the teachpoint.
     *
     * Adding that offset back puts the array centre where the box's array
     * centre is, which by the invariant aligns A1 with A1. The offset is half
     * the pitch for SBS labware.
     *
     * Rendering only — the motion path never reads the scene.
     *
     * Returns null when the mapping is not ready, so the caller falls back to
     * the mesh frame rather than drawing tips somewhere arbitrary.
     */
    _headTipMountCenterFromDeck(hostLink, pitchXMm, pitchYMm) {
        if (!hostLink || !this.labwareRoot || !this.deckMotionMap) return null;
        const deckPt = this._mapRobotXYToDeckLocal(
            Number(this.renderPositions?.X) || 0,
            Number(this.renderPositions?.Y) || 0,
        );
        if (!deckPt) return null;
        const world = this.labwareRoot.localToWorld(deckPt.clone());
        const local = hostLink.worldToLocal(world);
        if (!Number.isFinite(local.x) || !Number.isFinite(local.y)) return null;
        // The row axis is negated relative to the column axis: a box draws its
        // rows flipped (`displayRow = rows - 1 - row`) while the head does not,
        // so the same A1 offset applies with the opposite sign there.
        return {
            centerX: local.x + (pitchXMm / 2) / 1000,
            centerY: local.y - (pitchYMm / 2) / 1000,
        };
    }

    _getHeadTipMountFrame(hostLink) {
        if (!hostLink) return { centerX: 0, centerY: 0, minZ: -0.01 };
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
        if (!hasMesh) return { centerX: 0, centerY: 0, minZ: -0.01 };
        const minZ = localBox.min.z;
        const sliceThickness = 0.012;
        const bottomSlice = samplePoints.filter((p) => p.z <= (minZ + sliceThickness));
        const footprint = bottomSlice.length ? bottomSlice : samplePoints;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const p of footprint) {
            if (p.x < minX) minX = p.x;
            if (p.x > maxX) maxX = p.x;
            if (p.y < minY) minY = p.y;
            if (p.y > maxY) maxY = p.y;
        }
        return {
            centerX: Number.isFinite(minX) && Number.isFinite(maxX) ? (minX + maxX) / 2 : 0,
            centerY: Number.isFinite(minY) && Number.isFinite(maxY) ? (minY + maxY) / 2 : 0,
            minZ,
        };
    }

    _tipModelUrl(capacityUl) {
        const cap = Number(capacityUl || 0);
        if (cap > 0 && cap <= 10) return '/labware-assets/tips/d10.gltf?v=tips2';
        if (cap > 10) return '/labware-assets/tips/d30.gltf?v=tips2';
        // Default to d30 (30 uL) if unknown
        return '/labware-assets/tips/d30.gltf?v=tips2';
    }

    async _buildTipTemplate(url, targetLengthMm = 26.1) {
        const targetLengthM = Math.max(0.008, targetLengthMm / 1000);
        const cacheKey = `${url}|${targetLengthMm}`;
        if (!this.tipTemplateCache.has(cacheKey)) {
            this.tipTemplateCache.set(cacheKey, new Promise((resolve) => {
                this.gltfLoader.load(url, (gltf) => {
                    const wrapper = new THREE.Group();
                    const source = gltf.scene || gltf.scenes?.[0];
                    if (!source) { resolve(null); return; }
                    const clone = SkeletonUtils.clone(source);
                    clone.rotation.x = Math.PI / 2;
                    const bbox = new THREE.Box3().setFromObject(clone);
                    const size = bbox.getSize(new THREE.Vector3());
                    const sourceLength = Math.max(size.x, size.y, size.z, 1e-6);
                    const scale = targetLengthM / sourceLength;
                    clone.scale.setScalar(scale);
                    const finalBox = new THREE.Box3().setFromObject(clone);
                    const center = finalBox.getCenter(new THREE.Vector3());
                    clone.position.set(-center.x, -center.y, -finalBox.min.z);
                    clone.traverse(obj => {
                        if (!obj.isMesh) return;
                        obj.material = new THREE.MeshStandardMaterial({
                            color: 0xd9d9de, roughness: 0.85, metalness: 0.05,
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
        const template = await this.tipTemplateCache.get(cacheKey);
        return template ? template.clone(true) : null;
    }

    _buildSimpleTip(heightM = 0.02) {
        // Fallback: simple cone+cylinder tip geometry
        const group = new THREE.Group();
        const shaftH = heightM * 0.6;
        const tipH = heightM * 0.4;
        const shaftR = 0.0015;
        const shaftGeo = new THREE.CylinderGeometry(shaftR, shaftR, shaftH, 6);
        const tipGeo = new THREE.ConeGeometry(shaftR, tipH, 6);
        const mat = new THREE.MeshStandardMaterial({ color: 0xd9d9de, roughness: 0.85, metalness: 0.05 });
        const shaft = new THREE.Mesh(shaftGeo, mat);
        shaft.position.z = -shaftH / 2;
        shaft.rotation.x = Math.PI / 2;
        group.add(shaft);
        const tip = new THREE.Mesh(tipGeo, mat.clone());
        tip.position.z = -(shaftH + tipH / 2);
        tip.rotation.x = Math.PI / 2;
        group.add(tip);
        return group;
    }

    async _renderHeadTipsFromState() {
        this.headTipsRoot.clear();
        if (!this.urdfRobot || !this.tipsOnHead) return;

        const hostLink = this._getHeadTipHostLink();
        if (!hostLink) return;

        if (this.headTipsRoot.parent !== hostLink) {
            this.headTipsRoot.removeFromParent();
            hostLink.add(this.headTipsRoot);
        }

        const headType = this.headType || 'HT_384_D_70';
        const { geometry, selected } = this._selectedHeadCells(
            headType,
            this.tipsOnHeadMode || this.headMode,
        );
        const { pitchX, pitchY } = this._getHeadTipPitchMm(headType);
        const xOrigin = -((geometry.columns - 1) * pitchX) / 2;
        const yOrigin = -((geometry.rows - 1) * pitchY) / 2;
        const mountFrame = this._getHeadTipMountFrame(hostLink);
        // Prefer the deck-derived centre; the mesh frame is the fallback while
        // the deck mapping is still coming up. Z always comes from the mesh —
        // only the in-plane origin was wrong.
        const deckCenter = this._headTipMountCenterFromDeck(hostLink, pitchX, pitchY);
        const centerX = deckCenter ? deckCenter.centerX : mountFrame.centerX;
        const centerY = deckCenter ? deckCenter.centerY : mountFrame.centerY;
        const tipLengthMm = Number(this.attachedTipLengthMm || 26.1) || 26.1;
        const capacityUl = Number(this.activeTipCapacityUl || 30) || 30;
        const tipTemplate = await this._buildTipTemplate(this._tipModelUrl(capacityUl), tipLengthMm);
        const useGltf = !!tipTemplate;
        const tipHeightM = useGltf
            ? Number(tipTemplate.userData?.tipHeightM || Math.max(0.008, tipLengthMm / 1000))
            : Math.max(0.008, tipLengthMm / 1000);

        for (let row = 0; row < geometry.rows; row++) {
            for (let col = 0; col < geometry.columns; col++) {
                if (!selected.has(`${row}:${col}`)) continue;
                const tip = useGltf ? tipTemplate.clone(true) : this._buildSimpleTip(tipHeightM);
                tip.position.set(
                    centerX + ((xOrigin + col * pitchX) / 1000),
                    centerY + ((yOrigin + row * pitchY) / 1000),
                    mountFrame.minZ - tipHeightM + 0.002,
                );
                tip.traverse(obj => {
                    if (obj.isMesh && obj.material?.color) {
                        obj.material = obj.material.clone();
                        obj.material.color.setHex(0xe8e8ed);
                    }
                });
                this.headTipsRoot.add(tip);
            }
        }
    }

    async _attachTipboxTips(entry, detail) {
        const isTipBox = ['tip_box'].includes(String(detail?.base_class || detail?.kind || '').toLowerCase());
        if (!isTipBox) return;
        const geometry = this._getTipboxGeometry(detail);
        if (!geometry) return;
        // Per-cell state from a running workflow, when we have it. `undefined`
        // means nobody has told us anything about this box; an empty array
        // means "told, and nothing is hidden" — the two are not the same.
        const transient = this.tipboxTransientState?.[String(entry.location)];
        // A box configured empty used to return here without building any tip
        // meshes at all. That made returned tips impossible to show: there was
        // no geometry to reveal. Now the configured fill state is only the
        // fallback for when no per-cell state exists.
        if (transient === undefined && detail?.tipbox_fill_state === 'empty') return;
        const hiddenCells = new Set(transient || []);

        const capacityUl = Number(detail?.disposable_tip_capacity_ul || this.activeTipCapacityUl || 0);
        const tipLengthMm = this._tipLengthMmForDetail(detail, capacityUl);
        const tipsGroup = new THREE.Group();
        tipsGroup.name = 'tipbox-tips';
        const xOrigin = -((geometry.cols - 1) * geometry.pitchX) / 2;
        const yOrigin = -((geometry.rows - 1) * geometry.pitchY) / 2;

        const url = this._tipModelUrl(capacityUl);
        const tipTemplate = await this._buildTipTemplate(url, tipLengthMm);
        const useGltf = !!tipTemplate;
        const tipHeightM = useGltf
            ? Number(tipTemplate.userData?.tipHeightM || Math.max(0.008, tipLengthMm / 1000))
            : Math.max(0.008, tipLengthMm / 1000);
        const zOffset = Math.max(0, (geometry.heightMm / 1000) - tipHeightM);
        for (let row = 0; row < geometry.rows; row++) {
            for (let col = 0; col < geometry.cols; col++) {
                if (hiddenCells.has(`${row}:${col}`)) continue;
                const tip = useGltf ? tipTemplate.clone(true) : this._buildSimpleTip(tipHeightM);
                const displayRow = geometry.rows - 1 - row;
                tip.position.set(
                    (xOrigin + col * geometry.pitchX) / 1000,
                    (yOrigin + displayRow * geometry.pitchY) / 1000,
                    zOffset,
                );
                tipsGroup.add(tip);
            }
        }
        entry.group.add(tipsGroup);
    }

    /**
     * Set tips on/off the head using GLTF tip models.
     * @param {boolean} tipsOn - Whether tips should be visible
     * @param {string} [headType='HT_384_D_70'] - Head type for geometry lookup
     * @param {number} [capacityUl=30] - Tip capacity to select model (<=10 → d10, >10 → d30)
     * @param {number} [tipLengthMm=26.1] - Tip length in mm for scaling
     */
    async setHeadTips(tipsOn, headType = 'HT_384_D_70', capacityUl = 30, tipLengthMm = 26.1) {
        await this.setHeadTipState({
            headType,
            tipsOnHead: !!tipsOn,
            activeTipCapacityUl: capacityUl,
            attachedTipLengthMm: tipLengthMm,
            tipsOnHeadMode: this.tipsOnHeadMode || this.headMode,
        });
    }

    // ── Carry Animation ───────────────────────────────────────────────

    _resetCarryAnimation() {
        this.carryAnimation.active = false;
        this.carryAnimation.sourceLoc = null;
        this.carryAnimation.offset = null;
    }

    _getGripperCarryLocalPosition() {
        if (!this.urdfRobot?._links || !this.urdfRobot?.group) return null;
        const gripGroup =
            this.urdfRobot._links.gripperzaxis
            || this.urdfRobot._links.gripperzaxis_gripperzaxis
            || this.urdfRobot._links.fingerleft
            || this.urdfRobot._links.fingerright;
        if (!gripGroup) return null;
        const world = gripGroup.getWorldPosition(new THREE.Vector3());
        return this.urdfRobot.group.worldToLocal(world);
    }

    _isCarryAnimationStep(step) {
        return [
            // Real state machine step names
            'grip_plate', 'grip_plate_complete',
            'move_to_carry_height', 'move_to_carry_height_complete',
            'move_xy_to_place', 'move_xy_to_place_complete',
            'move_to_place_height', 'move_to_place_height_complete',
            'release_plate',
            // Workflow simulation step names
            'plate_picked', 'lift_to_carry', 'lower_to_place',
        ].includes(step);
    }

    _mapRobotXYToDeckLocal(xMm, yMm) {
        if (!this.deckMotionMap) return null;
        return new THREE.Vector3(
            this.deckMotionMap.x.scale * xMm + this.deckMotionMap.x.offset,
            this.deckMotionMap.y.scale * yMm + this.deckMotionMap.y.offset,
            0,
        );
    }

    _updateLabwareAnimation() {
        for (const entry of this.deckLabwareMeshes.values()) {
            entry.group.visible = true;
            entry.group.position.copy(entry.anchor);
        }
        const status = this.taskStatus || {};
        if (status.task !== 'pick_place' || !this._isCarryAnimationStep(status.step)) {
            this._resetCarryAnimation();
            return;
        }
        const fromLoc = Number(status.from_location || 0);
        const toLoc = Number(status.to_location || 0);
        const entry = this.deckLabwareMeshes.get(fromLoc);
        const destAnchor = this.deckSlotAnchors.get(toLoc);
        if (!entry || !destAnchor) {
            this._resetCarryAnimation();
            return;
        }
        const gripperLocal = this._getGripperCarryLocalPosition();
        const shouldFollow = status.step !== 'move_to_place_height_complete' && status.step !== 'release_plate';
        if (shouldFollow && gripperLocal) {
            if (!this.carryAnimation.active || this.carryAnimation.sourceLoc !== fromLoc || !this.carryAnimation.offset) {
                this.carryAnimation.active = true;
                this.carryAnimation.sourceLoc = fromLoc;
                this.carryAnimation.offset = entry.anchor.clone().sub(gripperLocal);
            }
            entry.group.position.copy(gripperLocal).add(this.carryAnimation.offset);
            return;
        }
        const pos = destAnchor.clone();
        pos.z += 0.001;
        entry.group.position.copy(pos);
    }

    // ── Animation Loop ────────────────────────────────────────────────

    _startAnimation() {
        const animate = () => {
            if (this._disposed) return;
            this._animationId = requestAnimationFrame(animate);
            const now = performance.now();
            const dt = Math.max(0.001, (now - this._lastAnimationFrameAt) / 1000);
            this._lastAnimationFrameAt = now;

            // Camera fly-to
            if (this.camAnim.active) {
                const t = Math.min((now - this.camAnim.startMs) / this.camAnim.durMs, 1);
                const k = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
                this.camera.position.lerpVectors(this.camAnim.fromPos, this.camAnim.toPos, k);
                this.controls.target.lerpVectors(this.camAnim.fromLook, this.camAnim.toLook, k);
                this.camera.lookAt(this.controls.target);
                if (t >= 1) {
                    this.camAnim.active = false;
                    this.controls.enableDamping = false;
                    this.controls.update();
                    this.controls.enableDamping = true;
                }
            } else {
                this.controls.update();
            }

            // Motion interpolation
            const lerpAlpha = 1 - Math.exp(-MOTION_ANIMATION.smoothingHz * dt);
            for (const axis of Object.keys(this.renderPositions)) {
                const hasTarget = Object.prototype.hasOwnProperty.call(this.motionTargets, axis);
                const target = hasTarget ? this.motionTargets[axis] : this.positions[axis];
                const current = this.renderPositions[axis];
                if (typeof target !== 'number' || typeof current !== 'number') continue;
                const delta = target - current;
                this.renderPositions[axis] =
                    Math.abs(delta) <= MOTION_ANIMATION.snapDistanceMm
                        ? target
                        : current + delta * lerpAlpha;
            }
            this._updateURDFJoints(this.renderPositions);
            this._updateLabwareAnimation();

            // Render
            this.renderer.render(this.scene, this.camera);
            if (this.gizmoRenderer) {
                this.gizmoCamera.position.set(0, 0, 3);
                this.gizmoCamera.position.applyQuaternion(this.camera.quaternion);
                this.gizmoCamera.lookAt(0, 0, 0);
                this.gizmoRenderer.render(this.gizmoScene, this.gizmoCamera);
            }
        };
        animate();
    }

    _updateURDFJoints(positions) {
        if (!this.urdfRobot) return;
        for (const [jointName, info] of Object.entries(JOINT_AXIS_MAP)) {
            const pos = positions[info.bravoAxis];
            if (pos === undefined) continue;
            const coupledPos = info.coupledAxis ? (positions[info.coupledAxis] ?? 0) : 0;
            const effectivePos = pos + coupledPos * (info.coupledScale ?? 0);
            const urdfVal = info.scale * (effectivePos - info.homeOffset) / 1000;
            if (this._debugSimulation && (jointName === 'zaxis' || jointName === 'zaxis-gripper')) {
                // Throttle: only log when positions change meaningfully
                const key = `_dbg_${jointName}`;
                if (Math.abs((this[key] || 0) - urdfVal) > 0.0001) {
                    this[key] = urdfVal;
                    console.log(`[URDF] ${jointName}: bravoPos=${pos.toFixed(2)}, coupled=${coupledPos.toFixed(2)}, effective=${effectivePos.toFixed(2)}, urdf=${urdfVal.toFixed(5)}`);
                }
            }
            this.urdfRobot.setJointValue(jointName, urdfVal);
        }
    }

    // ── Theme ─────────────────────────────────────────────────────────

    setBackground(color) {
        if (typeof color === 'string') {
            this.scene.background = new THREE.Color(color);
        } else {
            this.scene.background = color;
        }
    }
}

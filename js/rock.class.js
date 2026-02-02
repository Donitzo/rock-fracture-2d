import * as THREE from './libs/three.module.min.js';

const vertexShader = `
attribute vec4 state;
attribute vec3 _chunk_center;

uniform float time;

varying vec2 vUv;
varying vec2 vDirection;
varying float vOutline;
varying float vNormDepth;
varying float vFade;

void main() {
    vUv = uv.xy;

    vOutline = step(0.05, position.z);

    vNormDepth = color.g;

    float angle = normal.z * 6.28318530718;
    vDirection = vec2(cos(angle), sin(angle));

    float destroyedTime = state.x;
    vec2 velocity = state.yz;
    float angularVelocity = state.w;

    float t = max(time - destroyedTime, 0.0);

    vFade = smoothstep(1.6, 2.0, t);

    vec2 d = position.xy - _chunk_center.xy;

    float a = angularVelocity * t;
    float c = cos(a);
    float s = sin(a);
    d = mat2(c, -s, s, c) * d;
    vec3 p = position;
    p.xy = _chunk_center.xy + d + velocity * t;

    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}`;

const fragmentShader = `
uniform sampler2D map;

const vec2 lightDirection = vec2(0.7071068, -0.7071068);

varying vec2 vUv;
varying vec2 vDirection;
varying float vOutline;
varying float vNormDepth;
varying float vFade;

void main() {
    float d = dot(normalize(vDirection), normalize(lightDirection));
    float angleLight = d * 0.5 + 0.5;
    float depthLight = smoothstep(0.0, 0.3, vNormDepth);
    float lighting = depthLight * (0.5 + angleLight * 0.5);
    vec3 color = mix(texture2D(map, vUv).rgb * lighting, vec3(0.3), vOutline);
    gl_FragColor = vec4(color + vec3(vFade * 5.0), 1.0 - vFade);
}`;

export default class Rock extends THREE.Mesh {
    #chunksAlive = null;
    #metaData = null;
    #firstChunk = true;
    #destroyIn = 0;

    constructor(mesh, texture) {
        const geometry = mesh.geometry.clone();

        const vertexCount = geometry.attributes.position.count;
        const stateBuffer = new Float32Array(vertexCount * 4);

        const stateAttribute = new THREE.BufferAttribute(stateBuffer, 4);
        stateAttribute.setUsage(THREE.DynamicDrawUsage);

        geometry.setAttribute('state', stateAttribute);

        const material = new THREE.ShaderMaterial({
            vertexShader,
            fragmentShader,
            vertexColors: true,
            transparent: true,
            uniforms: {
                time: { value: 0 },
                map: { value: texture },
            }
        });

        super(geometry, material);

        this.#metaData = mesh.userData;

        this.#reset();
    }

    update(elapsedSeconds) {
        this.#destroyIn -= elapsedSeconds;
        if (this.#destroyIn < 0) {
            this.#destroyIn += Math.random() * 0.3;
            this.#destroyRandomChunk();
        }

        this.material.uniforms.time.value += elapsedSeconds;
    }

    #destroyRandomChunk() {
        if (this.#chunksAlive.length === 0) {
            this.#reset();
        }

        const aliveSet = new Set(this.#chunksAlive);

        const candidates = this.#chunksAlive.filter(chunkIndex => {
            const chunk = this.#metaData.chunks[chunkIndex];

            if (chunk.graph_depth === 0 && this.#firstChunk) {
                return true;
            }

            return chunk.connected_chunks.some(
                neighbor => !aliveSet.has(neighbor)
            );
        });

        this.#firstChunk = false;

        if (candidates.length === 0) {
            return;
        }

        const index = candidates[Math.floor(Math.random() * candidates.length)];

        this.#chunksAlive.splice(this.#chunksAlive.indexOf(index), 1);

        const chunk = this.#metaData.chunks[index];
        const stateAttribute = this.geometry.attributes.state;

        const start = chunk.vertex_offset;
        const count = chunk.vertex_count;

        const angle = Math.random() * Math.PI * 2;
        const velocity = Math.random() * 0.3;
        const angularVelocity = -2 + Math.random() * 4;

        for (let i = 0; i < count; i++) {
            const v = start + i;
            const o = v * 4;

            stateAttribute.array[o + 0] = this.material.uniforms.time.value;
            stateAttribute.array[o + 1] = Math.sin(angle) * velocity;
            stateAttribute.array[o + 2] = Math.cos(angle) * velocity;
            stateAttribute.array[o + 3] = angularVelocity;
        }

        stateAttribute.needsUpdate = true;
    }

    #reset() {
        this.#chunksAlive = this.#metaData.chunks.map((_, i) => i);
        this.#firstChunk = true;
        this.#destroyIn = 1;

        const stateAttribute = this.geometry.attributes.state;
        const array = stateAttribute.array;

        for (let i = 0; i < array.length; i += 4) {
            array[i + 0] = 1e9;
            array[i + 1] = 0;
            array[i + 2] = 0;
            array[i + 3] = 0;
        }

        stateAttribute.needsUpdate = true;
    }
}

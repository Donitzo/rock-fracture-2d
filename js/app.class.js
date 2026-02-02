import Rock from './rock.class.js';

import * as THREE from './libs/three.module.min.js';
import { GLTFLoader } from './libs/GLTFLoader.js';

export default class App {
    static {
        App.#init();
    }

    static async #init() {
        const [rocks, texture] = await Promise.all([
            new GLTFLoader().loadAsync('../rocks.glb'),
            new THREE.TextureLoader().loadAsync('../texture.png'),
        ]);

        texture.colorSpace = THREE.SRGBColorSpace;
        texture.wrapS = THREE.RepeatWrapping;
        texture.wrapT = THREE.RepeatWrapping;

        return new App(rocks, texture);
    }

    #clock = null;

    #renderer = null;

    #scene = null;

    #camera = null;

    #rocks = [];

    constructor(rocks, texture) {
        this.#clock = new THREE.Clock(false);

        this.#renderer = new THREE.WebGLRenderer({
            canvas: document.querySelector('canvas'),
            antialias: true,
        });

        this.#renderer.setPixelRatio(window.devicePixelRatio);

        this.#scene = new THREE.Scene();

        this.#camera = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.1, 2);
        this.#camera.position.z = 1;

        rocks.scene.children.forEach((child, i) => {
            if (child.isMesh) {
                const rock = new Rock(child, texture);
                rock.position.x = (i % 8) * 2.2 - 8;
                rock.position.y = Math.floor(i / 8) * 2 - 3;
                this.#scene.add(rock);
                this.#rocks.push(rock);
            }
        });

        window.addEventListener('resize', this.#handleResize.bind(this));

        this.#handleResize();

        this.#clock.start();

        this.#renderer.setAnimationLoop(this.#update.bind(this));
    }

    #handleResize() {
        this.#renderer.setSize(window.innerWidth, window.innerHeight);
        const aspect = window.innerHeight / window.innerWidth;
        this.#camera.left = -10;
        this.#camera.right = 10;
        this.#camera.top = 10 * aspect;
        this.#camera.bottom = -10 * aspect;
        this.#camera.updateProjectionMatrix();
    }

    #update(time, frame = null) {
        const elapsedSeconds = this.#clock.getDelta();

        this.#rocks.forEach(rock => {
            rock.update(elapsedSeconds);
        });

        this.#renderer.render(this.#scene, this.#camera);
    }
}

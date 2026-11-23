const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = readFileSync(join(__dirname, '..', 'viewer', 'studio.html'), 'utf8');
const script = html.match(/<script>\n([\s\S]*?)<\/script>/)[1];

function fakeElement() {
    return {
        children: [], style: {}, innerHTML: '', innerText: '', value: '',
        classList: { add() {}, remove() {}, toggle() {} },
        appendChild(child) { this.children.push(child); },
        remove() {},
    };
}

class Vector3 { set() { return this; } sub() { return this; } }
class Disposable { disposed = false; dispose() { this.disposed = true; } }

function loadStudio() {
    const elements = new Map(), loads = [], alerts = [];
    const document = {
        getElementById: id => elements.get(id) ?? elements.set(id, fakeElement()).get(id),
        createElement: fakeElement,
        querySelectorAll: () => [],
    };
    const context = vm.createContext({
        document, localStorage: { getItem: () => null }, window: {}, setTimeout() {},
        alert: message => alerts.push(message), console: { error() {} },
        THREE: {
            MeshStandardMaterial: Disposable, MeshNormalMaterial: Disposable, Vector3,
            Box3: class { setFromObject() { return this; } getCenter(v) { return v; } getSize(v) { return v; } },
            GLTFLoader: class { load(url, onLoad, onProgress, onError) { loads.push({ url, onLoad, onError }); } },
        },
    });
    vm.runInContext(script, context);
    vm.runInContext(`
        scene = { objects: new Set(), add(o) { this.objects.add(o); }, remove(o) { this.objects.delete(o); } };
        camera = { position: new THREE.Vector3() };
        controls = { target: new THREE.Vector3(), update() {} };`, context);
    return { context, elements, loads, alerts, run: code => vm.runInContext(code, context) };
}

function fakeModel() {
    const geometry = Object.assign(new Disposable(), { attributes: { position: { count: 3 } }, index: null });
    const texture = Object.assign(new Disposable(), { isTexture: true });
    const material = Object.assign(new Disposable(), { map: texture });
    const mesh = { isMesh: true, geometry, material, userData: {} };
    return { position: new Vector3(), userData: {}, parts: [geometry, material, texture],
        traverse(visit) { visit(this); visit(mesh); } };
}

const HOSTILE = '"><img src=x onerror=alert(1)>';
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
const tagShape = markup => markup.match(/<[^>]*>/g).map(tag => tag.replace(/"[^"]*"/g, '""'));
const visibleText = markup => markup.replace(/<[^>]*>/g, '').replace(/&(?:#(\d+)|#x([\da-f]+)|([a-z]+));/gi,
    (_, dec, hex, name) => dec ? String.fromCharCode(dec) : hex ? String.fromCharCode(parseInt(hex, 16)) : ENTITIES[name]);

function renderModelList(name) {
    const studio = loadStudio();
    studio.context.models = [{ name: name + '.glb', folder: name, category: name, size: 4, url: '/models/x.glb' }];
    studio.run('allModels = models; renderAccordionList();');
    const [group] = studio.elements.get('model-list').children;
    return group.children[0].innerHTML + group.children[1].children[0].innerHTML;
}

function renderTextureGrid(name) {
    const studio = loadStudio();
    studio.context.textures = Array.from({ length: 81 },
        () => ({ name: name + '.png', format: 'PNG Image', size: 4, preview: '/textures/x.png' }));
    studio.run('allTextures = textures; renderTexturesGrid();');
    const grid = studio.elements.get('textures-grid');
    grid.children.at(-1).onclick();
    const cards = grid.children.filter(child => child.className === 'tex-card');
    assert.equal(cards.length, 81);
    return cards[0].innerHTML + cards[80].innerHTML;
}

test('hostile model and texture file names render as text, not as elements or attributes', () => {
    for (const render of [renderModelList, renderTextureGrid]) {
        const hostile = render(HOSTILE);
        assert.deepEqual(tagShape(hostile), tagShape(render('plain name')));
        assert.ok(visibleText(hostile).includes(HOSTILE), `${render.name} must still show the name`);
    }
});

test('a download that finishes or fails after a later click does not replace the model last clicked', () => {
    const studio = loadStudio();
    studio.run(`loadModel('/models/a.glb', 'a.glb', '', 1);
        loadModel('/models/b.glb', 'b.glb', '', 1);
        loadModel('/models/c.glb', 'c.glb', '', 1);`);
    const [a, b, c] = studio.loads;
    const modelC = fakeModel();
    c.onLoad({ scene: modelC });
    b.onError(new Error('late failure'));
    a.onLoad({ scene: fakeModel() });
    assert.equal(studio.run('currentMesh'), modelC);
    assert.deepEqual([...studio.run('scene.objects')], [modelC]);
    assert.equal(studio.elements.get('stat-name').innerText, 'c.glb');
    assert.deepEqual(studio.alerts, []);
});

test('switching models in clay mode disposes the old model\'s geometry, material and texture, not the clay', () => {
    const studio = loadStudio();
    const [modelA, modelB] = [fakeModel(), fakeModel()];
    studio.run(`setShadingMode('clay'); loadModel('/models/a.glb', 'a.glb', '', 1);`);
    studio.loads[0].onLoad({ scene: modelA });
    studio.run(`loadModel('/models/b.glb', 'b.glb', '', 1);`);
    studio.loads[1].onLoad({ scene: modelB });
    assert.deepEqual(modelA.parts.map(part => part.disposed), [true, true, true]);
    assert.deepEqual(modelB.parts.map(part => part.disposed), [false, false, false]);
    assert.equal(studio.run('clayMaterial.disposed'), false);
});

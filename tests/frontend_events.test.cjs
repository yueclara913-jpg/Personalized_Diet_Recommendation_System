// Run with: node --test tests/frontend_events.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const flush = () => new Promise(resolve => setImmediate(resolve));

function page() {
    function element(id, tab) {
        const classes = new Set();
        return {
            id, dataset: { tab }, value: '', checked: false, style: {},
            children: [], listeners: {},
            classList: {
                toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
                contains(name) { return classes.has(name); }
            },
            addEventListener(type, callback) {
                (this.listeners[type] ||= []).push(callback);
            },
            fire(type) {
                for (const callback of this.listeners[type] || []) callback.call(this, { target: this });
            },
            appendChild(child) { this.children.push(child); },
            getContext() { return this; },
            set innerHTML(value) { this.html = value; this.text = ''; this.children = []; },
            get innerHTML() { return this.html || ''; },
            set textContent(value) { this.text = value; this.html = ''; this.children = []; },
            get textContent() { return this.text || ''; }
        };
    }
    const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
    const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)]
        .map(match => [match[1], element(match[1])]));
    const tabs = ['user-info', 'recommendations', 'visualization'].map(tab => element('', tab));
    const close = element('close');
    const document = {
        callbacks: [],
        addEventListener(type, callback) {
            assert.equal(type, 'DOMContentLoaded');
            this.callbacks.push(callback);
        },
        getElementById(id) { assert.ok(elements[id], id); return elements[id]; },
        querySelector() { return close; },
        querySelectorAll(selector) {
            return selector === '.tab-button' ? tabs : tabs.map(tab => elements[tab.dataset.tab]);
        },
        createElement() { return element(''); }
    };
    const requests = [], charts = [], windowListeners = {};
    let response = {};
    let networkError = null;
    let jsonError = false;
    const context = vm.createContext({
        document,
        window: { addEventListener(type, callback) { windowListeners[type] = callback; } },
        fetch(url, options) {
            requests.push({ url, options });
            return networkError ? Promise.reject(networkError) : Promise.resolve({
                json: () => jsonError ? Promise.reject(new SyntaxError('Invalid JSON')) : Promise.resolve(response)
            });
        },
        Chart: class {
            constructor(canvas, config) {
                assert.ok(!charts.some(chart => chart.canvas === canvas && !chart.destroyed), 'canvas already in use');
                this.canvas = canvas; this.config = config; charts.push(this);
            }
            destroy() { this.destroyed = true; }
        }
    });
    // Execute exactly the local scripts referenced by the real template.
    for (const match of html.matchAll(/filename='(js\/[^']+)'/g)) {
        vm.runInContext(fs.readFileSync(path.join(root, 'static', match[1]), 'utf8'), context);
    }
    // A stale inclusion of the legacy script must not bind another handler.
    vm.runInContext(fs.readFileSync(path.join(root, 'static/js/script.js'), 'utf8'), context);
    for (const callback of document.callbacks) callback();
    Object.assign(elements.age, { value: '22' });
    elements.weight.value = '70'; elements.height.value = '175';
    elements.gender.value = 'Male'; elements['activity-level'].value = 'Lightly Active';
    elements.goal.value = 'Weight Loss';
    return { elements, tabs, close, requests, charts, windowListeners,
        respond(data) { response = data; networkError = null; jsonError = false; },
        fail() { networkError = new Error('offline'); },
        invalidJSON() { networkError = null; jsonError = true; } };
}

test('template loads main.js exactly once and legacy script registers no events', () => {
    const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
    const scripts = [...html.matchAll(/filename='(js\/[^']+)'/g)].map(match => match[1]);
    assert.deepEqual(scripts, ['js/main.js']);
    vm.runInNewContext(fs.readFileSync(path.join(root, 'static/js/script.js'), 'utf8'), {
        document: { addEventListener() { assert.fail('legacy document listener'); } },
        window: { addEventListener() { assert.fail('legacy window listener'); } }
    });
    const p = page();
    for (const id of ['calculate-bmi', 'generate-recommendations', 'view-visualization']) {
        assert.equal(p.elements[id].listeners.click.length, 1);
    }
    for (const tab of p.tabs) assert.equal(tab.listeners.click.length, 1);
});

test('BMI category boundaries and API/network errors are handled', async () => {
    const p = page();
    for (const [bmi, category] of [[18.49, 'Underweight'], [18.5, 'Normal'], [24.99, 'Normal'],
        [25, 'Overweight'], [29.99, 'Overweight'], [30, 'Obese']]) {
        const before = p.requests.length;
        p.respond({ bmi }); p.elements['calculate-bmi'].fire('click'); await flush();
        assert.equal(p.requests.length, before + 1);
        assert.equal(p.elements.bmi.value, `${bmi} (${category})`);
    }
    p.respond({ error: 'Invalid BMI input' });
    p.elements['calculate-bmi'].fire('click'); await flush();
    assert.equal(p.elements['error-message'].textContent, 'Invalid BMI input');
    p.fail(); p.elements['calculate-bmi'].fire('click'); await flush();
    assert.match(p.elements['error-message'].textContent, /offline/);
    for (const value of ['', '0', '-1', 'not-a-number']) {
        p.elements.weight.value = value;
        const before = p.requests.length;
        p.elements['calculate-bmi'].fire('click');
        assert.equal(p.requests.length, before);
    }
});

test('repeated recommendations replace results and reject invalid required fields', async () => {
    const p = page();
    const food = { Food_items: 'Tofu', Category: 'Protein', Calories: 100, Protein: 10, Carbohydrates: 5, Fats: 4, Fibre: 1 };
    const nutrition_req = { calories: 1800, protein: 100, carbs: 200, fat: 60, fiber: 25 };
    for (const foods of [Array(5).fill(food), [], [food]]) {
        p.respond({ nutrition_req, meal_plan: { breakfast: foods, lunch: foods, dinner: foods, snacks: foods } });
        const before = p.requests.length;
        p.elements['generate-recommendations'].fire('click'); await flush();
        assert.equal(p.requests.length, before + 1);
        for (const meal of ['breakfast', 'lunch', 'dinner', 'snacks']) {
            assert.equal(p.elements[meal].children.length, foods.length);
            if (foods.length) assert.equal(p.elements[meal].textContent, '');
            else assert.match(p.elements[meal].textContent, /No food items/);
        }
    }
    for (const id of ['age', 'weight', 'height']) {
        const original = p.elements[id].value;
        for (const value of ['', '0', '-1']) {
            p.elements[id].value = value;
            const before = p.requests.length;
            p.elements['generate-recommendations'].fire('click');
            assert.equal(p.requests.length, before);
            assert.equal(p.elements['loading-overlay'].style.display, 'none');
        }
        p.elements[id].value = original;
    }
});

test('malformed JSON and visualization network failures show errors without creating charts', async () => {
    const p = page();
    p.invalidJSON();
    p.elements['generate-recommendations'].fire('click'); await flush();
    assert.equal(p.requests.length, 1);
    assert.equal(p.elements['loading-overlay'].style.display, 'none');
    assert.match(p.elements['error-message'].textContent, /Invalid JSON/);
    p.tabs[2].fire('click'); await flush();
    assert.equal(p.requests.length, 2);
    assert.equal(p.charts.length, 0);
    assert.match(p.elements['error-message'].textContent, /Invalid JSON/);
    p.fail(); p.elements['view-visualization'].fire('click'); await flush();
    assert.equal(p.requests.length, 3);
    assert.equal(p.charts.length, 0);
    assert.match(p.elements['error-message'].textContent, /offline/);
});

test('BMI click and input change each make one request; category and validation work', async () => {
    const p = page();
    p.respond({ bmi: 22.86 });
    p.elements['calculate-bmi'].fire('click');
    await flush();
    assert.equal(p.requests.length, 1);
    assert.equal(p.requests[0].url, '/calculate_bmi');
    assert.deepEqual(JSON.parse(p.requests[0].options.body), { weight: 70, height: 175 });
    assert.equal(p.elements.bmi.value, '22.86 (Normal)');
    p.elements.weight.fire('change'); await flush();
    assert.equal(p.requests.length, 2);
    p.elements.height.fire('change'); await flush();
    assert.equal(p.requests.length, 3);
    p.elements.height.value = '';
    p.elements['calculate-bmi'].fire('click');
    assert.equal(p.requests.length, 3);
    assert.equal(p.elements['error-modal'].style.display, 'block');
});

test('recommendation click makes one request, renders all meals, and preserves UI interactions', async () => {
    const p = page();
    const food = { Food_items: 'Tofu', Category: 'Protein', Calories: 100, Protein: 10, Carbohydrates: 5, Fats: 4, Fibre: 1 };
    p.elements.vegan.checked = true; p.elements.vegan.fire('change');
    assert.equal(p.elements.vegetarian.checked, true);
    p.respond({ nutrition_req: { calories: 1800, protein: 100, carbs: 200, fat: 60, fiber: 25 },
        meal_plan: { breakfast: [food], lunch: [food], dinner: [food], snacks: [] } });
    p.elements['generate-recommendations'].fire('click');
    assert.equal(p.elements['loading-overlay'].style.display, 'flex');
    await flush();
    assert.equal(p.requests.length, 1);
    assert.equal(p.requests[0].url, '/generate_recommendations');
    assert.equal(JSON.parse(p.requests[0].options.body).vegan, true);
    assert.match(p.elements['nutrition-requirements'].innerHTML, /1800/);
    for (const meal of ['breakfast', 'lunch', 'dinner']) {
        assert.equal(p.elements[meal].children.length, 1);
        assert.match(p.elements[meal].children[0].innerHTML, /Tofu/);
    }
    assert.match(p.elements.snacks.textContent, /No food items/);
    assert.ok(p.elements.recommendations.classList.contains('active'));
    assert.equal(p.elements['loading-overlay'].style.display, 'none');
    p.tabs[0].fire('click');
    assert.equal(p.requests.length, 1);
    assert.ok(p.elements['user-info'].classList.contains('active'));
    p.elements.vegetarian.checked = false; p.elements.vegetarian.fire('change');
    assert.equal(p.elements.vegan.checked, false);
    p.elements.age.value = ''; p.elements['generate-recommendations'].fire('click');
    assert.equal(p.requests.length, 1);
    p.close.fire('click'); assert.equal(p.elements['error-modal'].style.display, 'none');
    p.elements.age.value = '22'; p.respond({ error: 'No foods match' });
    p.elements['generate-recommendations'].fire('click'); await flush();
    assert.equal(p.elements['error-message'].textContent, 'No foods match');
    assert.equal(p.elements['loading-overlay'].style.display, 'none');
    p.windowListeners.click({ target: p.elements['error-modal'] });
    assert.equal(p.elements['error-modal'].style.display, 'none');
    p.fail(); p.elements['generate-recommendations'].fire('click'); await flush();
    assert.match(p.elements['error-message'].textContent, /offline/);
    assert.equal(p.elements['loading-overlay'].style.display, 'none');
});

test('visualization tab and button each request once; three charts rebuild without canvas conflicts', async () => {
    const p = page();
    p.respond({ macros: { labels: ['Protein', 'Carbs', 'Fat'], values: [400, 800, 540] },
        meal_calories: { labels: ['breakfast'], values: [300] },
        nutrient_comparison: { nutrients: ['Protein'], recommended: [100], actual: [90] } });
    p.tabs[2].fire('click'); await flush();
    assert.equal(p.requests.length, 1);
    assert.equal(p.requests[0].url, '/get_visualizations_data');
    assert.deepEqual(p.charts.map(chart => chart.config.type), ['pie', 'bar', 'bar']);
    p.elements['view-visualization'].fire('click'); await flush();
    assert.equal(p.requests.length, 2);
    assert.equal(p.charts.length, 6);
    assert.ok(p.charts.slice(0, 3).every(chart => chart.destroyed));
    assert.ok(p.elements.visualization.classList.contains('active'));
    p.respond({ error: 'Generate recommendations first' });
    p.tabs[2].fire('click'); await flush();
    assert.equal(p.requests.length, 3);
    assert.equal(p.charts.length, 6);
    assert.equal(p.elements['error-message'].textContent, 'Generate recommendations first');
});

# preact 文档（Context7 抓取，评估语料）


## Topic: useState and hooks

### Function component state with useState hook

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/04-state.md

Function component using the useState hook from preact/hooks. Calling setClicked(true) updates the state and causes Preact to re-render the component, changing the button text.

```jsx
import { useState } from 'preact/hooks';

const MyButton = () => {
	const [clicked, setClicked] = useState(false);

	const handleClick = () => {
		setClicked(true);
	};

	return (
		<button onClick={handleClick}>
			{clicked ? 'Clicked' : 'No clicks yet'}
		</button>
	);
};
```

--------------------------------

### Hook call site ordering with useState slots

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/04-state.md

Illustrates call site ordering of hooks: each useState call is associated with a slot. Hooks must be called in the same order and cannot be conditional or inside loops.

```js
function User() {
	const [name, setName] = useState('Bob');    // slot 0
	const [age, setAge] = useState(42);         // slot 1
	const [online, setOnline] = useState(true); // slot 2
}
```

--------------------------------

### useState counter example

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/hooks.md

Demonstrates the useState hook with a functional component. The setter accepts a value or a callback with the current state, and calling it triggers a rerender when the state changes.

```jsx
// --repl
import { render } from 'preact';
// --repl-before
import { useState } from 'preact/hooks';

const Counter = () => {
	const [count, setCount] = useState(0);
	const increment = () => setCount(count + 1);
	// You can also pass a callback to the setter
	const decrement = () => setCount(currentCount => currentCount - 1);

	return (
		<div>
			<p>Count: {count}</p>
			<button onClick={increment}>Increment</button>
			<button onClick={decrement}>Decrement</button>
		</div>
	);
};
// --repl-after
render(<Counter />, document.getElementById('app'));
```

### State > State in function components using hooks

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/04-state.md

### State in function components using hooks

Function components can have state too! While they don't have a
`this.state` property like class components, a tiny add-on module
is included with Preact that provides functions for storing
and working with state inside function components, called "hooks".

Hooks are special functions that can be called from within a function
component. They're special because they **remember information across
renders**, a bit like properties and methods on a class. For example,
the `useState` hook returns an Array containing a value and a "setter"
function that can be called to update that value. When a component is
invoked (re-rendered) multiple times, any `useState()` calls it makes
will return the exact same Array each time.

--------------------------------

### Debug Warnings and Errors > Hook can only be invoked from render methods

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/debugging.md

### Hook can only be invoked from render methods

This error occurs when you try to use a hook outside of a component. They are only supported inside a function component.

```jsx
// Invalid, must be used inside a component
const [value, setValue] = useState(0);

// valid
function Foo() {
	const [value, setValue] = useState(0);
	return <button onClick={() => setValue(value + 1)}>{value}</button>;
}
```

## Topic: signals

### signal(initialValue) - Create a signal

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/signals.md

Creates a new signal with the given initial value. The returned signal has a .value property for reading and writing.

```javascript
const count = signal(0);
```

--------------------------------

### computed(fn) - Create a computed signal

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/signals.md

Creates a read-only signal that automatically updates when any signals accessed within the callback change.

```javascript
const name = signal('Jane');
const surname = signal('Doe');

const fullName = computed(() => `${name.value} ${surname.value}`);
```

--------------------------------

### Computed signals can depend on other computed signals

Source: https://github.com/preactjs/preact-www/blob/master/content/en/blog/signal-boosting.md

Computed signals are themselves signals and can depend on other computed signals. This chain of computed signals updates reactively when the underlying signal changes.

```javascript
import { signal, computed } from '@preact/signals-core';
// --repl-before
const count = signal(1);
const double = computed(() => count.value * 2);
const quadruple = computed(() => double.value * 2);

console.log(quadruple.value); // Console: 4
count.value = 20;
console.log(quadruple.value); // Console: 80
```

--------------------------------

### effect(fn) - Run side effects on signal changes

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/signals.md

Runs a callback whenever accessed signals change. If the callback returns a function, it runs before the next update. Does not return a signal.

```javascript
const name = signal('Jane');

// Log to console when `name` changes:
effect(() => console.log('Hello', name.value));
// Logs: "Hello Jane"

name.value = 'John';
// Logs: "Hello John"
```

### Introducing Signals

Source: https://github.com/preactjs/preact-www/blob/master/content/en/blog/introducing-signals.md

Signals are a way of expressing state that ensure apps stay fast regardless of how complex they get. Signals are based on reactive principles and provide excellent developer ergonomics, with a unique implementation optimized for Virtual DOM.

At its core, a signal is an object with a `.value` property that holds some value. Accessing a signal's value property from within a component automatically updates that component when the value of that signal changes.

In addition to being straightforward and easy to write, this also ensures state updates stay fast regardless of how many components your app has. Signals are fast by default, automatically optimizing updates behind the scenes for you.

## Topic: virtual dom

### Virtual DOM object structure

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/01-vdom.md

Shows a simple Virtual DOM tree description using an object with type, props, and children. Used to illustrate how the Virtual DOM is a simple description of a tree structure.

```js
let vdom = {
	type: 'p',             // a <p> element
	props: {
		class: 'big',      // with class="big"
		children: [
			'Hello World!' // and the text "Hello World!"
		]
	}
};
```

--------------------------------

### createElement example

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/01-vdom.md

Shows how to use Preact's createElement to build a Virtual DOM tree and render it into the document body. The first argument is the HTML element name, the second is the props object, and additional arguments are children.

```jsx
import { createElement, render } from 'preact';

let vdom = createElement(
	'p',              // a <p> element
	{ class: 'big' }, // with class="big"
	'Hello World!'    // and the text "Hello World!"
);

render(vdom, document.body);
```

--------------------------------

### Component.render(props, state)

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/api-reference.md

All components must provide a render() function. The render function is passed the component's current props and state and should return a Virtual DOM Element, an Array, or null.

```APIDOC
## Component.render(props, state)

### Description
All components must provide a `render()` function. The render function is passed the component's current props and state, and should return a Virtual DOM Element (typically a JSX "element"), an Array, or `null`.

### Method
Component method

### Parameters
- **props** (object) - Required - The component's current props.
- **state** (object) - Required - The component's current state.

### Return Value
- A Virtual DOM Element, an Array, or `null`.

### Example
```jsx
import { Component } from 'preact';

class MyComponent extends Component {
	render(props, state) {
		// props is the same as this.props
		// state is the same as this.state

		return <h1>Hello, {props.name}!</h1>;
	}
}
```
```

### Virtual DOM

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/01-vdom.md

You might have heard people refer to "Virtual DOM", and wondered:
what makes it "virtual"? How is a "virtual" DOM different from
the real DOM we use when programming for the browser?

A Virtual DOM is a simple description of a tree structure using objects:

```js
let vdom = {
	type: 'p',             // a <p> element
	props: {
		class: 'big',      // with class="big"
		children: [
			'Hello World!' // and the text "Hello World!"
		]
	}
};
```

Libraries like Preact provide a way to construct these descriptions, which can
then be compared against the browser's DOM tree. As each part of the tree is
compared, and the browser's DOM tree is updated to match the structure described
by the Virtual DOM tree.

This is a useful tool, because it lets us compose user interfaces _declaratively_
rather than _imperatively_. Instead of describing _how_ to update the DOM in
response to things like keyboard or mouse input, we only need to describe _what_
the DOM should look like after that input is received. It means we can repeatedly
give Preact descriptions of tree structures, and it will update the browser's DOM
tree to match each new description – regardless of its current structure.

In this chapter, we'll learn how to create Virtual DOM trees, and how to tell
Preact to update the DOM to match those trees.

--------------------------------

### Virtual DOM > Creating Virtual DOM trees

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/01-vdom.md

### Creating Virtual DOM trees

There are a few ways to create Virtual DOM trees:

- `createElement()`: a function provided by Preact
- [JSX]: HTML-like syntax that can be compiled to JavaScript
- [HTM]: HTML-like syntax you can write directly in JavaScript

It's useful to start things off with the simplest approach, which would be to call Preact's `createElement()` function directly:

```jsx
import { createElement, render } from 'preact';

let vdom = createElement(
	'p',              // a <p> element
	{ class: 'big' }, // with class="big"
	'Hello World!'    // and the text "Hello World!"
);

render(vdom, document.body);
```

The code above creates a Virtual DOM "description" of a paragraph element.
The first argument to createElement is the HTML element name.
The second argument is the element's "props" - an object containing attributes
(or properties) to set on the element.
Any additional arguments are children for the element, which can be strings (like
`'Hello World!'`) or Virtual DOM elements from additional `createElement()` calls.

The last line tells Preact to build a real DOM tree that matches our Virtual DOM
"description", and to insert that DOM tree into the `<body>` of a web page.

## Topic: components and props

### Typing a function component with props interface

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/typescript.md

Define props for a function component using an interface and destructure them in the function signature.

```tsx
interface MyComponentProps {
	name: string;
	age: number;
}

function MyComponent({ name, age }: MyComponentProps) {
	return (
		<div>
			My name is {name}, I am {age.toString()} years old.
		</div>
	);
}
```

--------------------------------

### Rewrite MyButton to use children prop

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/03-components.md

Shows a component that renders its children inside a button, and an App component that uses it with an image and text.

```jsx
function MyButton(props) {
	return <button class="my-button">{props.children}</button>;
}

function App() {
	return (
		<MyButton>
			<img src="icon.png" />
			Click Me!
		</MyButton>
	);
}

render(<App />, document.body);
```

--------------------------------

### Define a class component with render method

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/03-components.md

Extends Preact's Component class and defines a render method that takes props and returns a Virtual DOM tree. This is the basic structure for a class component.

```jsx
import { Component } from 'preact';

class MyButton extends Component {
	render(props) {
		return <button class="my-button">{props.children}</button>;
	}
}

render(<MyButton>Click Me!</MyButton>, document.body);
```

--------------------------------

### Specifying children with ComponentChildren

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/typescript.md

Use ComponentChildren to explicitly type the children prop in a function component.

```tsx
import { h, ComponentChildren } from 'preact';

interface ChildrenProps {
	title: string;
	children: ComponentChildren;
}

function Card({ title, children }: ChildrenProps) {
	return (
		<div class="card">
			<h1>{title}</h1>
			{children}
		</div>
	);
}
```

--------------------------------

### Functional Component Example

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/components.md

Defines a functional component that receives props and renders a div. The component name must start with an uppercase letter. The example includes usage and a render call.

```jsx
// --repl
import { render } from 'preact';

// --repl-before
function MyComponent(props) {
	return <div>My name is {props.name}.</div>;
}

// Usage
const App = <MyComponent name="John Doe" />;

// Renders: <div>My name is John Doe.</div>
render(App, document.body);
```

## Topic: diffing algorithm

### dangerouslySetInnerHTML skips diffing of children

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/upgrade-guide.md

When a vnode has `dangerouslySetInnerHTML` set, Preact will skip diffing its children.

```jsx
<div dangerouslySetInnerHTML="foo">
	<span>I will be skipped</span>
	<p>So will I</p>
</div>
```

--------------------------------

### hydrate() usage

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/api-reference.md

Switches from render() to hydrate() to skip most diffing when loading pre-rendered HTML, while still attaching event listeners and setting up the component tree.

```jsx
// --repl
import { hydrate } from 'preact';

const Foo = () => <div>foo</div>;
hydrate(<Foo />, document.getElementById('container'));
```

--------------------------------

### TodoList component without keys

Source: https://github.com/preactjs/preact-www/blob/master/content/en/tutorial/08-keys.md

A simple to-do list component that renders a list of items from state. Clicking the button removes the first item, demonstrating how Preact's default list diffing can be suboptimal when items are removed from the beginning.

```jsx
export default function TodoList() {
	const [todos, setTodos] = useState(['wake up', 'make bed']);

	function wakeUp() {
		setTodos(['make bed']);
	}

	return (
		<div>
			<ul>
				{todos.map(todo => (
					<li>{todo}</li>
				))}
			</ul>
			<button onClick={wakeUp}>I'm Awake!</button>
		</div>
	);
}
```

### Releases > 10.16.0

Source: https://github.com/preactjs/preact-www/blob/master/content/en/blog/preact-x.md

In our research for v11 we went deep on child diffing as we were aware that there were a few cases where our current algorithm would fall short, just listing a few
of these issues:

- [removing an element before another would cause re-insertion](https://github.com/preactjs/preact/issues/3973)
- [re-insertiosn when removing more than 1 child](https://github.com/preactjs/preact/issues/2622)
- [unnecessary unmounting of keyed nodes](https://github.com/preactjs/preact/issues/2783)

Not all of these resulted in a bad state, some just meant decreased performance... When we found out that we could port skew-based diffing to Preact X we
were thrilled, not only would we fix a lot of cases we could see how this algorithm behaves in the wild! Which in retrospect, it did great, at times I would
have wished we had good testbeds to run these on first rather than our community having to report issues. I do want to use this opportunity to thank you all
for helping us out by always filing considerate issues with reproductions, you all are the absolute best!

--------------------------------

### Preact X, a story of stability

Source: https://github.com/preactjs/preact-www/blob/master/content/en/blog/preact-x.md

Preact has a much bigger userbase today compared to when we made the original plans for v11. It enjoys wide usage in many small to big companies for mission critical software. We really want to be sure that any breaking changes we may introduce are absolutely worth the cost of moving the whole ecosystem over to it.

As we were [experimenting](https://github.com/preactjs/preact/tree/v11) we went a new type of diffing, named
[skew based diffing](https://github.com/preactjs/preact/pull/3388), we saw real performance
improvements as well as it fixing a bunch of long-running bugs. As time went on and we invested more time in
these experiments for Preact 11, we started noticing that the improvements we were landing didn't need to be exclusive to Preact 11.

## Topic: react compatibility

### Import map aliasing React to Preact compat

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/no-build-workflows.md

An import map that maps 'react', 'react/', 'react-dom', and '@mui/material' to Preact compat and esm.sh URLs. The '@mui/material' entry uses '?external=react,react-dom' to avoid duplicating React/Preact.

```html
<script type="importmap">
	{
		"imports": {
			"preact": "https://esm.sh/preact@10.23.1",
			"preact/": "https://esm.sh/preact@10.23.1/",
			"react": "https://esm.sh/preact@10.23.1/compat",
			"react/": "https://esm.sh/preact@10.23.1/compat/",
			"react-dom": "https://esm.sh/preact@10.23.1/compat",
			"@mui/material": "https://esm.sh/@mui/material@5.16.7?external=react,react-dom"
		}
	}
</script>
```

--------------------------------

### Node package.json aliases for Preact

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/getting-started.md

Use npm aliases in package.json dependencies to replace react and react-dom with @preact/compat when running in Node, where bundler aliases do not work (e.g., NextJS).

```json
{
	"dependencies": {
		"react": "npm:@preact/compat",
		"react-dom": "npm:@preact/compat"
	}
}
```

--------------------------------

### Parcel package.json alias configuration for Preact

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/getting-started.md

Configure Parcel to alias React to Preact by adding an alias key in package.json.

```json
{
	"alias": {
		"react": "preact/compat",
		"react-dom/test-utils": "preact/test-utils",
		"react-dom": "preact/compat",
		"react/jsx-runtime": "preact/jsx-runtime"
	}
}
```

### Version Compatibility

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/differences-to-react.md

For both preact and [preact/compat], version compatibility is measured against the _current_ and _previous_ major releases of React. When new features are announced by the React team, they may be added to Preact's core if it makes sense given the [Project Goals]. This is a fairly democratic process, constantly evolving through discussion and decisions made in the open, using issues and pull requests.

> Thus, the website and documentation reflect React `15.x` through `17.x`, with some `18.x` and `19.x` additions, when discussing compatibility or making comparisons.

--------------------------------

### preact/compat

Source: https://github.com/preactjs/preact-www/blob/master/content/en/guide/v10/api-reference.md

## preact/compat

`preact/compat` is our compatibility layer that allows you to use Preact as a drop-in replacement for React. It provides all of the APIs of `preact` and `preact/hooks`, whilst also providing a few more to match the React API.
---
applyTo: '**'
description: 'Angular instructions'
---

# Angular Instructions

Follow these instructions whenever you are working with Angular.

## Specific Information

Whenever you are generating a new component, you MUST create a corresponding HTML file, SCSS file, and Cypress spec file.

- Avoid using `effect` unless absolutely necessary. Prefer using `computed` and `linkedSignal` instead as they will make your components easier to reason about and test.
- Private class functions MUST be placed at the end of the class after all public and protected functions.
- All `input()` properties should be grouped together at the top of the class
- All `output()` properties should be grouped together below the `input()` properties
- Order for types:
  - Inputs
  - Outputs
  - ViewChild / ViewChildren
  - ContentChild / ContentChildren
  - Public properties
  - Protected properties
  - Getters / Setters
  - Constructor
  - Public methods
  - Protected methods
  - Private properties
  - Private methods

## Examples

### Angular 19 Examples

These are modern examples of how to write an Angular 19 component with signals

```ts
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';

@Component({
  selector: 'sonar-note',
  templateUrl: 'note.component.html',
  styleUrls: ['note.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class NoteComponent {
  protected readonly noteText = input.required<string>();
  protected readonly extraText = input<string | undefined>();
  protected readonly isServerRunning = signal(true);

  constructor() {}

  toggleServerStatus() {
    this.isServerRunning.update((isServerRunning) => !isServerRunning);
  }

  onSave() {
    this.someAdvancedSaveLogic();
  }

  onDelete() {
    this.someAdvancedDeleteFunction();
  }

  private someAdvancedSaveLogic() {
    // some complex logic worth extracting to here
  }

  private someAdvancedDeleteFunction() {
    // some complex logic worth extracting to here
  }
}
```

```css
.container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100vh;

  button {
    margin-top: 10px;
  }
}
```

```html
<section class="container" data-test-id="{{tag-name}}-container">
  @if (isServerRunning()) {
  <span data-test-id="{{tag-name}}-status-running">Yes, the server is running</span>
  } @else {
  <span data-test-id="{{tag-name}}-status-stopped">No, the server is not running</span>
  }
  <button (click)="toggleServerStatus()" data-test-id="{{tag-name}}-toggle-status-button"> Toggle Server Status </button>
</section>
```

```ts
const config = {
  componentProperties: {}
};

const {
{
  lowerClassName
}
}
StatusRunning = selectByDataTestId('{{tag-name}}-status-running');
const {
{
  lowerClassName
}
}
StatusStopped = selectByDataTestId('{{tag-name}}-status-stopped');
const {
{
  lowerClassName
}
}
ToggleStatusButton = selectByDataTestId('{{tag-name}}-toggle-status-button');

describe({
{
  ClassName
}
}.
name, () => {
  beforeEach(() => {
    cy.mount({
    {
      ClassName
    }
  },
    config
  )
    ;
  });

  describe(`toggling the server status`, () => {
    it(`should show the server is running by default`, () => {
      // given
      // when
      // then
      cy.get({
      {
        lowerClassName
      }
    }
      StatusRunning
    ).
      should('exist');
      cy.get({
      {
        lowerClassName
      }
    }
      StatusStopped
    ).
      should('not.exist');
    });
    it(`should show the server is not running when toggled`, () => {
      // given
      // when
      cy.get({
      {
        lowerClassName
      }
    }
      ToggleStatusButton
    ).
      click();

      // then
      cy.get({
      {
        lowerClassName
      }
    }
      StatusRunning
    ).
      should('not.exist');
      cy.get({
      {
        lowerClassName
      }
    }
      StatusStopped
    ).
      should('exist');
    });

    it(`should show the server is running when toggled twice`, () => {
      // given
      cy.get({
      {
        lowerClassName
      }
    }
      ToggleStatusButton
    ).
      click();

      // when
      cy.get({
      {
        lowerClassName
      }
    }
      ToggleStatusButton
    ).
      click();

      // then
      cy.get({
      {
        lowerClassName
      }
    }
      StatusRunning
    ).
      should('exist');
      cy.get({
      {
        lowerClassName
      }
    }
      StatusStopped
    ).
      should('not.exist');
    });

  });
}
)
;
```

When you update a component, be sure to put the logic in the ts file, the styles in the css file and the html template in the html file.

## Resources

### Angular Essentials

Here are the some links to the essentials for building Angular applications. Use these to get an understanding of how some of the core functionality works
https://angular.dev/essentials/components
https://angular.dev/essentials/signals
https://angular.dev/essentials/templates
https://angular.dev/essentials/dependency-injection

### Nx Essentials

Here are some links to the Nx documentation
https://nx.dev/getting-started/intro
https://nx.dev/features/run-tasks
https://nx.dev/features/generate-code
https://nx.dev/concepts/how-caching-works
https://nx.dev/concepts/sync-generators
https://nx.dev/packages/angular/overview
https://nx.dev/technologies/test-tools/cypress/introduction
https://nx.dev/technologies/test-tools/cypress/recipes/cypress-component-testing
https://nx.dev/extending-nx/tutorials/tooling-plugin#create-an-application-generator
https://nx.dev/extending-nx/recipes/composing-generators
https://nx.dev/extending-nx/recipes/creating-files

## Best practices & Style guide

Here are the best practices and the style guide information.

### Coding Style guide

Here is a link to the most recent Angular style guide https://angular.dev/style-guide

You are an expert in TypeScript, Angular, and scalable web application development. You write functional, maintainable, performant, and accessible code following Angular and TypeScript best practices.

### TypeScript Best Practices

- Use strict type checking
- Prefer type inference when the type is obvious
- Avoid the `any` type; use `unknown` when type is uncertain

### Angular Best Practices

- Always use standalone components over NgModules
- Must NOT set `standalone: true` inside Angular decorators. It's the default in Angular v20+.
- Use signals for state management
- Implement lazy loading for feature routes
- Do NOT use the `@HostBinding` and `@HostListener` decorators. Put host bindings inside the `host` object of the `@Component` or `@Directive` decorator instead
- Use `NgOptimizedImage` for all static images.
  - `NgOptimizedImage` does not work for inline base64 images.

### Accessibility Requirements

- It MUST pass all AXE checks.
- It MUST follow all WCAG AA minimums, including focus management, color contrast, and ARIA attributes.

#### Components

- Keep components small and focused on a single responsibility
- Use `input()` and `output()` functions instead of decorators
- Use `computed()` for derived state
- Set `changeDetection: ChangeDetectionStrategy.OnPush` in `@Component` decorator
- Prefer inline templates for small components
- Prefer Reactive forms instead of Template-driven ones
- Do NOT use `ngClass`, use `class` bindings instead
- Do NOT use `ngStyle`, use `style` bindings instead
- When using external templates/styles, use paths relative to the component TS file.

### State Management

- Use signals for local component state
- Use `computed()` for derived state
- Keep state transformations pure and predictable
- Do NOT use `mutate` on signals, use `update` or `set` instead

### Templates

- Keep templates simple and avoid complex logic
- Use native control flow (`@if`, `@for`, `@switch`) instead of `*ngIf`, `*ngFor`, `*ngSwitch`
- Use the async pipe to handle observables
- Do not assume globals like (`new Date()`) are available.
- Do not write arrow functions in templates (they are not supported).
- Do not write Regular expressions in templates (they are not supported).

### Services

- Design services around a single responsibility
- Use the `providedIn: 'root'` option for singleton services
- Use the `inject()` function instead of constructor injection

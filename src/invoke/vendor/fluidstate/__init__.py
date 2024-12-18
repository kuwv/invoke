# Copyright (c) 2022 Jesse P. Johnson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Compact statechart that can be vendored."""

import inspect
import logging
from copy import deepcopy
from itertools import chain, zip_longest
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

__author__ = 'Jesse P. Johnson'
__author_email__ = 'jpj6652@gmail.com'
__title__ = 'fluidstate'
__description__ = 'Compact statechart that can be vendored.'
__version__ = '1.3.0a2'
__license__ = 'MIT'
__copyright__ = 'Copyright 2022 Jesse Johnson.'
__all__ = ('StateChart', 'State', 'Transition')

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

Content = Union[Callable, str]
Condition = Union[Content, bool]


def tuplize(value: Any) -> Tuple[Any, ...]:
    """Convert any type into a tuple."""
    return tuple(value) if type(value) in (list, tuple) else (value,)


class Action:
    """Encapsulate executable content."""

    def __init__(self, content: 'Content') -> None:
        self.content = content

    @classmethod
    def create(
        cls, settings: Union['Action', Callable, Dict[str, Any]]
    ) -> 'Action':
        """Create expression from configuration."""
        if isinstance(settings, cls):
            return settings
        if callable(settings) or isinstance(settings, str):
            return cls(settings)
        if isinstance(settings, dict):
            return cls(**settings)
        raise InvalidConfig('could not find a valid configuration for action')

    def run(
        self,
        machine: 'StateChart',
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run the action."""
        if callable(self.content):
            return self.__run_with_args(self.content, machine, *args, **kwargs)
        return self.__run_with_args(
            getattr(machine, self.content), *args, **kwargs
        )

    @staticmethod
    def __run_with_args(content: Callable, *args: Any, **kwargs: Any) -> Any:
        signature = inspect.signature(content)
        if len(signature.parameters.keys()) != 0:
            return content(*args, **kwargs)
        return content()


class Guard:
    """Control the flow of transitions to states with conditions."""

    def __init__(self, condition: 'Condition') -> None:
        self.condition = condition

    @classmethod
    def create(
        cls, settings: Union['Guard', Callable, Dict[str, Any], bool]
    ) -> 'Guard':
        """Create expression from configuration."""
        if isinstance(settings, cls):
            return settings
        if callable(settings) or isinstance(settings, str):
            return cls(settings)
        if isinstance(settings, dict):
            return cls(**settings)
        if isinstance(settings, bool):
            return cls(condition=settings)
        raise InvalidConfig('could not find a valid configuration for guard')

    def evaluate(
        self, machine: 'StateChart', *args: Any, **kwargs: Any
    ) -> bool:
        """Evaluate conditions."""
        if callable(self.condition):
            return self.condition(machine, *args, **kwargs)
        if isinstance(self.condition, str):
            cond = getattr(machine, self.condition)
            if callable(cond):
                if len(dict((inspect.signature(cond)).parameters).keys()) != 0:
                    return cond(*args, **kwargs)
                return cond()
            return bool(cond)
        if isinstance(self.condition, bool):
            return self.condition
        return False


class Transition:
    """Provide transition capability for transitions."""

    def __init__(
        self,
        event: str,
        target: str,
        action: Optional[Iterable['Action']] = None,
        cond: Optional[Iterable['Guard']] = None,
    ) -> None:
        self.event = event
        self.target = target
        self.action = action
        self.cond = cond

    def __repr__(self) -> str:
        return repr(f"Transition(event={self.event}, target={self.target})")

    @classmethod
    def create(cls, settings: Union['Transition', dict]) -> 'Transition':
        """Consolidate."""
        if isinstance(settings, cls):
            return settings
        if isinstance(settings, dict):
            return cls(
                event=settings['event'],
                target=settings['target'],
                action=(
                    tuple(map(Action.create, tuplize(settings['action'])))
                    if 'action' in settings
                    else []
                ),
                cond=(
                    tuple(map(Guard.create, tuplize(settings['cond'])))
                    if 'cond' in settings
                    else []
                ),
            )
        raise InvalidConfig('could not find a valid transition configuration')

    def callback(self) -> Callable:
        """Provide callback capbility."""

        def event(machine: 'StateChart', *args: Any, **kwargs: Any) -> None:
            machine._process_transitions(self.event, *args, **kwargs)

        event.__name__ = self.event
        event.__doc__ = f"Show event: '{self.event}'."
        return event

    def evaluate(
        self, machine: 'StateChart', *args: Any, **kwargs: Any
    ) -> bool:
        """Evaluate guard conditions to determine correct transition."""
        result = True
        if self.cond:
            for cond in self.cond:
                result = cond.evaluate(machine, *args, **kwargs)
                if not result:
                    break
        return result

    def run(self, machine: 'StateChart', *args: Any, **kwargs: Any) -> None:
        """Execute actions of the transition."""
        machine._change_state(self.target)
        if self.action:
            for action in self.action:
                action.run(machine, *args, **kwargs)
            log.info("executed action event for %r", self.event)
        else:
            log.info("no action event for %r", self.event)


class State:  # pylint: disable=too-many-instance-attributes
    """Represent state."""

    __initial: Optional['Content']
    __on_entry: Optional[Iterable['Action']]
    __on_exit: Optional[Iterable['Action']]
    __stack: List['State']
    __superstate: Optional['State']

    def __init__(
        self,
        name: str,
        transitions: Optional[List['Transition']] = None,
        states: Optional[List['State']] = None,
        **kwargs: Any,
    ) -> None:
        if not name.replace('_', '').isalnum():
            raise InvalidConfig('state name contains invalid characters')
        self.name = name
        self.__superstate: Optional['State'] = None
        self.__type = kwargs.get('type')
        self.__initial = kwargs.get('initial')
        self.__substates = {}
        for state in states or []:
            state.superstate = self
            self.__substates[state.name] = state
        self.__transitions = transitions or []
        for transition in self.transitions:
            self.__register_transition_callback(transition)
        # FIXME: pseudostates should not include triggers
        self.__on_entry = kwargs.get('on_entry')
        self.__on_exit = kwargs.get('on_exit')
        self.__validate_state()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, State):
            return self.name == other.name
        if isinstance(other, str):
            return self.name == other
        return False

    def __repr__(self) -> str:
        return repr(f"State({self.name})")

    def __str__(self) -> str:
        return f"State({self.name})"

    def __iter__(self) -> 'State':
        self.__stack = [self]
        return self

    def __next__(self) -> 'State':
        # simple breadth-first iteration
        if self.__stack:
            x = self.__stack.pop()
            if isinstance(x, State):
                self.__stack = list(
                    # XXX: why is chain appending in reverse?!?
                    chain(reversed(x.substates.values()), self.__stack)
                )
            return x
        raise StopIteration

    def __reversed__(self) -> Iterator['State']:
        target: Optional['State'] = self
        while target:
            yield target
            target = target.superstate

    def __register_transition_callback(self, t: 'Transition') -> None:
        # XXX: currently mapping to class instead of instance
        # TODO: need way to provide auto-transition
        setattr(
            self,
            t.event if t.event != '' else '_auto_',
            # pylint: disable-next=unnecessary-dunder-call
            t.callback().__get__(self, self.__class__),
        )

    def __validate_state(self) -> None:
        # TODO: empty statemachine should default to null event
        if self.type == 'compound':
            if len(self.__substates) < 2:
                raise InvalidConfig(
                    'There must be at least two states', self.name
                )
            # if not self.initial:
            #     raise InvalidConfig('There must exist an initial state')
        if self.type == 'final' and self.__on_exit:
            log.warning('final state will never run "on_exit" action')
        log.info('evaluated state')

    @classmethod
    def create(cls, settings: Union['State', dict, str]) -> 'State':
        """Consolidate."""
        if isinstance(settings, cls):
            return settings
        if isinstance(settings, str):
            return cls(settings)
        if isinstance(settings, dict):
            return settings.get('factory', cls)(
                name=settings['name'],
                initial=settings.get('initial'),
                type=settings.get('type'),
                states=(
                    list(map(State.create, settings.pop('states')))
                    if 'states' in settings
                    else None
                ),
                transitions=(
                    list(map(Transition.create, settings['transitions']))
                    if 'transitions' in settings
                    else []
                ),
                on_entry=(
                    tuple(map(Action.create, tuplize(settings['on_entry'])))
                    if 'on_entry' in settings
                    else None
                ),
                on_exit=(
                    tuple(map(Action.create, tuplize(settings['on_exit'])))
                    if 'on_exit' in settings
                    else []
                ),
            )
        raise InvalidConfig('could not find a valid state configuration')

    @property
    def initial(self) -> Optional['Content']:
        """Return initial substate if defined."""
        return self.__initial

    @property
    def type(self) -> str:
        """Return state type."""
        if self.__type:
            return self.__type
        if self.substates:
            return 'compound'
        return 'atomic'

    @property
    def path(self) -> str:
        """Get the statepath of this state."""
        return '.'.join(reversed([x.name for x in reversed(self)]))

    @property
    def substates(self) -> Dict[str, 'State']:
        """Return substates."""
        return self.__substates or {}

    @property
    def superstate(self) -> Optional['State']:
        """Get superstate state."""
        return self.__superstate

    @superstate.setter
    def superstate(self, state: 'State') -> None:
        if self.__superstate is None:
            self.__superstate = state
        else:
            raise FluidstateException('cannot change superstate for state')

    @property
    def transitions(self) -> Tuple['Transition', ...]:
        """Return transitions of this state."""
        return tuple(self.__transitions)

    def _run_on_entry(self, machine: 'StateChart') -> None:
        if self.__on_entry is not None:
            for action in self.__on_entry:
                action.run(machine)
                log.info(
                    "executed 'on_entry' state change action for %s", self.name
                )

    def _run_on_exit(self, machine: 'StateChart') -> None:
        if self.__on_exit is not None:
            for action in self.__on_exit:
                action.run(machine)
                log.info(
                    "executed 'on_exit' state change action for %s", self.name
                )


class MetaStateChart(type):
    """Provide capability to populate configuration for statemachine ."""

    _root: 'State'

    def __new__(
        mcs,
        name: str,
        bases: Tuple[type, ...],
        attrs: Dict[str, Any],
    ) -> 'MetaStateChart':
        obj = super().__new__(mcs, name, bases, attrs)
        if '__statechart__' in attrs:
            settings = attrs.pop('__statechart__')
            obj._root = settings.pop('factory', State)(
                name=settings.pop('name', 'root'),
                initial=settings.pop('initial', None),
                type=settings.pop('type', None),
                states=(
                    list(map(State.create, settings.pop('states')))
                    if 'states' in settings
                    else None
                ),
                transitions=(
                    list(map(Transition.create, settings.pop('transitions')))
                    if 'transitions' in settings
                    else None
                ),
            )
        return obj


class StateChart(metaclass=MetaStateChart):
    """Provide state management capability."""

    __initial: 'State'

    def __init__(
        self,
        initial: Optional[Union[Callable, str]] = None,
        **kwargs: Any,
    ) -> None:
        if 'logging_enabled' in kwargs and kwargs['logging_enabled']:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    fmt=' %(name)s :: %(levelname)-8s :: %(message)s'
                )
            )
            log.addHandler(handler)
            if 'logging_level' in kwargs:
                log.setLevel(kwargs['logging_level'].upper())
        log.info('initializing statemachine')

        if hasattr(self.__class__, '_root'):
            self.__state = deepcopy(self.__class__._root)
        else:
            raise InvalidConfig(
                'attempted initialization with empty superstate'
            )

        current = initial or self._root.initial
        if current:
            self.__state = self.get_state(
                current(self) if callable(current) else current
            )
        elif self.states:
            self.__state = self.states[0]
        else:
            raise InvalidConfig('an initial state must exist for statechart')
        log.info('loaded states and transitions')

        if kwargs.get('enable_start_transition', True):
            self.__state._run_on_entry(self)
            self.__process_transient_state()
        log.info('statemachine initialization complete')

    def __getattr__(self, name: str) -> Any:
        # ignore private attribute lookups
        if name.startswith('__'):
            raise AttributeError

        # handle state check for active states
        if name.startswith('is_'):
            return name[3:] in self.active

        # if self.state.type == 'final':
        #     raise InvalidTransition('final state cannot transition')

        for t in self.transitions:
            if t.event == name or (t.event == '' and name == '_auto_'):
                # pylint: disable-next=unnecessary-dunder-call
                return t.callback().__get__(self, self.__class__)
        raise AttributeError(f"unable to find {name!r} attribute")

    @property
    def active(self) -> Tuple['State', ...]:
        """Return active states."""
        return tuple(reversed(self.state))

    @property
    def transitions(self) -> Iterator['Transition']:
        """Return list of current transitions."""
        for state in self.active:
            yield from state.transitions

    @property
    def superstate(self) -> 'State':
        """Return superstate."""
        return self.state.superstate or self._root

    @property
    def states(self) -> Tuple['State', ...]:
        """Return list of states."""
        return tuple(self.superstate.substates.values())

    @property
    def state(self) -> 'State':
        """Return the current state."""
        return self.__state

    def get_relpath(self, target: str) -> str:
        """Get relative statepath of target state to current state."""
        if target in ('', self.state):  # self reference
            relpath = '.'
        else:  # need to determine if state is ascendent of descendent
            path = ['']
            source_path = self.state.path.split('.')
            target_path = self.get_state(target).path.split('.')
            for i, x in enumerate(
                zip_longest(source_path, target_path, fillvalue='')
            ):  # match the paths to walk either up or down from current
                if x[0] != x[1]:
                    if x[0] != '':  # target is a descendent
                        path.extend(['' for x in source_path[i:]])
                    if x[1] == '':  # target is a ascendent
                        path.extend([''])
                    if x[1] != '':  # target is child of a ascendent
                        path.extend(target_path[i:])
                    if i == 0:
                        raise InvalidState(
                            f"no relative path exists for: {target!s}"
                        )
                    break
            relpath = '.'.join(path)
        return relpath

    def get_state(self, statepath: str) -> 'State':
        """Get state."""
        state: 'State' = self._root
        macrostep = statepath.split('.')

        # general recursive search for single query
        if len(macrostep) == 1:
            for x in list(state):
                if x == macrostep[0]:
                    return x
        # set start point if using relative lookup
        elif statepath.startswith('.'):
            relative = len(statepath) - len(statepath.lstrip('.')) - 1
            state = self.active[relative:][0]
            rel = relative + 1
            macrostep = [state.name] + macrostep[rel:]

        # check relative lookup is done
        target = macrostep[-1]
        if target in ('', state):
            return state

        # path based search
        while state and macrostep:
            microstep = macrostep.pop(0)
            # skip if current state is at microstep
            if state == microstep:
                continue
            # return current state if target found
            if state == target:
                return state
            # walk path if exists
            if hasattr(state, 'states') and microstep in state.states.keys():
                state = state.states[microstep]
                # check if target is found
                if not macrostep:
                    return state
            else:
                break
        raise InvalidState(f"state could not be found: {statepath}")

    def get_transitions(self, event: str) -> Tuple['Transition', ...]:
        """Get each transition maching event."""
        return tuple(
            filter(
                lambda transition: transition.event == event, self.transitions
            )
        )

    def _change_state(self, statepath: str) -> None:
        """Traverse statepath."""
        relpath = self.get_relpath(statepath)
        if relpath == '.':  # handle self transition
            self.state._run_on_exit(self)
            self.state._run_on_entry(self)
        else:
            s = 2 if relpath.endswith('.') else 1  # stupid black
            macrostep = relpath.split('.')[s:]
            for microstep in macrostep:
                try:
                    if microstep == '':  # reverse
                        self.state._run_on_exit(self)
                        self.__state = self.active[1]
                    elif (
                        isinstance(self.state, State)
                        and microstep in self.state.substates.keys()
                    ):  # forward
                        state = self.state.substates[microstep]
                        self.__state = state
                        state._run_on_entry(self)
                    else:
                        raise InvalidState(
                            f"statepath not found: {statepath!r}"
                        )
                except FluidstateException as err:
                    log.error(err)
                    raise KeyError(
                        f"superstate is undefined for {statepath!r}"
                    ) from err
        log.info('changed state to %s', statepath)

    def transition(
        self, event: str, statepath: Optional[str] = None
    ) -> Optional[Any]:
        """Transition from one state to another."""
        s = self.get_state(statepath) if statepath else self.state
        for t in s.transitions:
            if t.event == event:
                # pylint: disable-next=unnecessary-dunder-call
                return t.callback().__get__(self, self.__class__)
        return None

    def __process_transient_state(self) -> None:
        for x in self.state.transitions:
            if x.event == '':
                self._auto_()
                break

    def _process_transitions(
        self, event: str, *args: Any, **kwargs: Any
    ) -> None:
        # TODO: need to consider superstate transitions.
        transitions = self.get_transitions(event)
        if not transitions:
            raise InvalidTransition('no transitions match event')
        transition = self.__evaluate_guards(transitions, *args, **kwargs)
        transition.run(self, *args, **kwargs)
        log.info('processed transition event %s', transition.event)

    def __evaluate_guards(
        self, transitions: Tuple['Transition', ...], *args: Any, **kwargs: Any
    ) -> 'Transition':
        allowed = []
        for transition in transitions:
            if transition.evaluate(self, *args, **kwargs):
                allowed.append(transition)
        if not allowed:
            raise GuardNotSatisfied(
                'Guard is not satisfied for this transition'
            )
        if len(allowed) > 1:
            raise ForkedTransition(
                'More than one transition was allowed for this event'
            )
        log.info('processed guard for %s', allowed[0].event)
        return allowed[0]


class FluidstateException(Exception):
    """Provide base fluidstate exception."""


class InvalidConfig(FluidstateException):
    """Handle invalid state configuration."""


class InvalidTransition(FluidstateException):
    """Handle invalid transitions."""


class InvalidState(FluidstateException):
    """Handle invalid state transition."""


class GuardNotSatisfied(FluidstateException):
    """Handle failed guard check."""


class ForkedTransition(FluidstateException):
    """Handle multiple possible transiion paths."""

"""Maintain named views with incremental updates."""
from parser import Parser
from binder import bind
from engine import optimize, execute
from model import DomainError
from copy import deepcopy

class ViewManager:
    def __init__(self, database):
        self.database = deepcopy(database)
        self.tables = {}  # dict: table_name -> {rows: [...], id_map: {id: idx}, used_ids: set}
        self.views = {}  # dict: view_name -> {sql, optimize, cached_result, affected_tables}
        self.revision = 0
        self.initialize_tables()
    
    def initialize_tables(self):
        for name, table_spec in self.database.items():
            row_ids = {}
            used_ids = set()
            for i in range(len(table_spec['rows'])):
                rid = i + 1
                row_ids[rid] = i
                used_ids.add(rid)
            self.tables[name] = {
                'id_map': row_ids,  # maps id -> current index in rows
                'used_ids': used_ids,  # set of all IDs that have ever been used
                'next_id': len(table_spec['rows']) + 1
            }
    
    def _extract_affected_tables(self, query):
        """Extract table names referenced in a query."""
        affected = set()
        for source_name, _ in query.sources:
            affected.add(source_name)
        return affected
    
    def create_view(self, name, sql, optimize_flag):
        if name in self.views:
            raise DomainError('VIEW_EXISTS')
        
        # Parse and bind the query using current database state
        plan = bind(Parser(sql).parse(), self.database)
        
        # Execute the query to get initial result
        base_plan = plan
        if optimize_flag:
            base_plan = optimize(deepcopy(plan))
        result = execute(base_plan)
        
        # Track which tables this view references
        affected_tables = self._extract_affected_tables(plan.query)
        
        self.views[name] = {
            'sql': sql,
            'optimize': optimize_flag,
            'result': result,
            'revision': self.revision,
            'affected_tables': affected_tables
        }
        
        return name, self.revision
    
    def read_view(self, name):
        if name not in self.views:
            raise DomainError('UNKNOWN_VIEW')
        
        view = self.views[name]
        return {
            'revision': self.revision,
            'columns': view['result']['columns'],
            'rows': view['result']['rows']
        }
    
    def drop_view(self, name):
        if name not in self.views:
            raise DomainError('UNKNOWN_VIEW')
        del self.views[name]
        return name
    
    def apply_changes(self, changes):
        # Validate all changes first (without modifying state)
        affected_tables = set()
        for change in changes:
            op = change.get('op')
            table = change.get('table')
            
            if op not in ('insert', 'update', 'delete'):
                raise DomainError('INVALID_COMMAND')
            
            if not isinstance(table, str) or table not in self.database:
                raise DomainError('UNKNOWN_TABLE')
            
            affected_tables.add(table)
            
            row_id = change.get('id')
            if not isinstance(row_id, int) or row_id <= 0 or row_id > 2147483647:
                raise DomainError('INVALID_COMMAND')
            
            if op in ('insert', 'update'):
                row = change.get('row')
                if not isinstance(row, list):
                    raise DomainError('INVALID_COMMAND')
                
                table_spec = self.database[table]
                if len(row) != len(table_spec['columns']):
                    raise DomainError('INVALID_ROW')
                
                for v, c in zip(row, table_spec['columns']):
                    if v is None:
                        if not c['nullable']:
                            raise DomainError('INVALID_ROW')
                    else:
                        col_type = c['type']
                        # Check type - be strict about bool vs int since bool is subclass of int
                        if col_type == 'int':
                            if not isinstance(v, int) or isinstance(v, bool):
                                raise DomainError('INVALID_ROW')
                            if v < -1000000000 or v > 1000000000:
                                raise DomainError('INVALID_ROW')
                        elif col_type == 'text':
                            if not isinstance(v, str):
                                raise DomainError('INVALID_ROW')
                        elif col_type == 'bool':
                            if not isinstance(v, bool):
                                raise DomainError('INVALID_ROW')
            
            # Check ID state
            table_info = self.tables[table]
            if op == 'insert':
                # Row ID can never be reused, even if previously deleted
                if row_id in table_info['used_ids']:
                    raise DomainError('ROW_ID_USED')
            else:  # update or delete
                if row_id not in table_info['id_map']:
                    raise DomainError('UNKNOWN_ROW')
        
        # All validations passed, apply changes
        for change in changes:
            op = change['op']
            table = change['table']
            row_id = change['id']
            table_info = self.tables[table]
            table_spec = self.database[table]
            
            if op == 'insert':
                # Append to rows
                table_spec['rows'].append(change['row'])
                # Map new ID to new index
                table_info['id_map'][row_id] = len(table_spec['rows']) - 1
                table_info['used_ids'].add(row_id)
                table_info['next_id'] = max(table_info['next_id'], row_id + 1)
            elif op == 'update':
                row_idx = table_info['id_map'][row_id]
                table_spec['rows'][row_idx] = change['row']
            elif op == 'delete':
                row_idx = table_info['id_map'][row_id]
                # Remove from rows
                table_spec['rows'].pop(row_idx)
                # Update all id_map entries for rows after the deleted one
                new_id_map = {}
                for rid, idx in table_info['id_map'].items():
                    if rid == row_id:
                        continue
                    # Shift down indices for rows after deleted row
                    new_idx = idx if idx < row_idx else idx - 1
                    new_id_map[rid] = new_idx
                table_info['id_map'] = new_id_map
                # Note: used_ids is NOT cleared, so the ID is reserved forever
        
        # Update only affected views - avoid re-evaluating unaffected views
        self.revision += 1
        for view_name, view in self.views.items():
            # Only re-evaluate views that reference modified tables
            if view['affected_tables'] & affected_tables:
                # Re-parse and bind the view query
                plan = bind(Parser(view['sql']).parse(), self.database)
                
                # Execute
                if view['optimize']:
                    plan = optimize(plan)
                result = execute(plan)
                
                view['result'] = result
            
            view['revision'] = self.revision
        
        return self.revision

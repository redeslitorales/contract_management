# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProjectTask(models.Model):
    _inherit = 'project.task'

    @api.model
    def create(self, vals):
        """Override create to check if installation task is being scheduled"""
        task = super(ProjectTask, self).create(vals)
        task._check_installation_scheduled()
        return task

    def write(self, vals):
        """Override write to check if installation task is being scheduled"""
        res = super(ProjectTask, self).write(vals)
        # Only check if planned_date_begin is being SET (not just changed)
        # This prevents triggering on reschedules
        if 'planned_date_begin' in vals and vals.get('planned_date_begin'):
            self._check_installation_scheduled()
        return res

    def _check_installation_scheduled(self):
        """
        Check if this is an installation task that just got scheduled.
        If so, advance the installation state from 'to_be_scheduled' to 'scheduled'.
        
        State progression rules:
        - Only advance from 'to_be_scheduled' → 'scheduled'
        - Never regress or change state if not at 'to_be_scheduled'
        """
        for task in self:
            # Only process installation tasks with a sale order
            if not task.sale_order_id:
                continue
            
            # Check if this is an installation task
            if not task.fsm_task_type_id or not task.fsm_task_type_id.is_installation:
                continue
            
            # Check if task now has a scheduled date
            if not task.planned_date_begin:
                continue
            
            # CRITICAL: Only advance if currently at 'to_beschedule' state
            # This prevents regression from later states
            current_state = task.sale_order_id.installation_state
            if current_state != 'to_be_scheduled':
                # Don't touch subscriptions that are not at the schedule state
                continue
            
            # Advance subscription to next state (Pending Install)
            task.sale_order_id.write({'installation_state': 'scheduled'})
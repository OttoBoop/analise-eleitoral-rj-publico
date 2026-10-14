import test from 'node:test';
import assert from 'node:assert/strict';
import {colorValue,scaleMax,adaptiveNumber,BLUES,ORANGES,NO_DATA,ZERO} from '../web/colors.js';
test('small real limits retain full contrast without a one-point floor',()=>{assert.equal(scaleMax([null,.003,-.009]),.009);assert.equal(colorValue(.009,.009,BLUES), 'rgb(8,48,107)');assert.equal(colorValue(-.009,.009,BLUES,true),'rgb(127,39,4)');assert.notEqual(colorValue(.001,.009),colorValue(.002,.009));});
test('different candidates use different hue at the same value and scale',()=>{assert.notEqual(colorValue(30,60,BLUES),colorValue(30,60,ORANGES));assert.equal(colorValue(null,60),NO_DATA);assert.equal(colorValue(0,60),ZERO);});
test('tiny differences cannot be rounded to zero and all-zero scales are defined',()=>{for(const n of [.004,.0000004,-.006,1e-12])assert.notEqual(Number(adaptiveNumber(n).replace(',','.')),0);assert.equal(scaleMax([0,null]),0);assert.equal(colorValue(0,0),ZERO);assert.equal(adaptiveNumber(null),'—');});
test('signed zero is shown as neutral exact zero without suppressing real negative differences',()=>{assert.equal(adaptiveNumber(-0),'0,00');assert.equal(colorValue(-0,50,BLUES,true),ZERO);assert.equal(adaptiveNumber(-.0004),'-0,0004');});
